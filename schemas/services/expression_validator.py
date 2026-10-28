"""
Expression Validator Module

Validates ``{{ expression }}`` token strings stored by the ``expression``
datatype. The attribute value is a plain string whose tokens are evaluated
by the consumer at render time; this validator checks token syntax at
authoring time (fail-fast) so malformed expressions are rejected during
import/validate instead of silently degrading downstream.

Accepted value forms:

- A string whose whole body is one token: ``"{{flag ? 'a' : 'b'}}"``.
- Text with inline tokens: ``"Total: {{wallet.count ?? 0}} items"``.
- Text without tokens: a plain literal (always valid).
- Non-string scalars (bool/int/float): degenerate literals — the consumer
  accepts them as literal values and storage coerces them to text.

Per-token grammar (syntax-only validation — nothing is evaluated here;
operand types resolve in the consumer runtime)::

    expr         := nullCoalesce ('?' expr ':' expr)?
    nullCoalesce := orExpr ('??' orExpr)*
    orExpr       := andExpr ('||' andExpr)*
    andExpr      := equality ('&&' equality)*
    equality     := comparison (('==' | '!=') comparison)*
    comparison   := unary (('>' | '<' | '>=' | '<=') unary)?
    unary        := '!' unary | primary
    primary      := literal | path | '(' expr ')'
    path         := ident ('.' segment)*   — segment: ident | integer index
    literal      := 'string' | number | 'true' | 'false' | 'null'

Plugin contract (see ``datatype_plugins``): ``validate_expression_value``
raises only ``django.core.exceptions.ValidationError`` for invalid input;
any other exception type is a bug in the validator.
"""

from enum import Enum, auto
from typing import Any, List

from django.core.exceptions import ValidationError

# Token delimiters. '{{' coincides with template-engine syntax but these
# values are data, not templates — no collision.
EXPRESSION_OPEN = '{{'
EXPRESSION_CLOSE = '}}'

# Configurable token sets — the same grammar surface the consumer
# implements, derived here so docs and validation cannot drift.
ALLOWED_COMPARISON_OPERATORS = frozenset({'>', '<', '>=', '<='})
ALLOWED_EQUALITY_OPERATORS = frozenset({'==', '!='})
ALLOWED_LOGIC_OPERATORS = frozenset({'&&', '||'})
ALLOWED_UNARY_OPERATORS = frozenset({'!'})
ALLOWED_COALESCE_OPERATORS = frozenset({'??'})
ALLOWED_EXPRESSION_OPERATORS = frozenset(
    ALLOWED_COMPARISON_OPERATORS
    | ALLOWED_EQUALITY_OPERATORS
    | ALLOWED_LOGIC_OPERATORS
    | ALLOWED_UNARY_OPERATORS
    | ALLOWED_COALESCE_OPERATORS
)
KEYWORD_LITERALS = frozenset({'true', 'false', 'null'})

_WHITESPACE = frozenset({' ', '\t', '\n', '\r'})


class _TokenType(Enum):
    """Lexer token kinds for the expression grammar."""

    QUESTION = auto()        # ?
    COLON = auto()           # :
    COALESCE = auto()        # ??
    OR = auto()              # ||
    AND = auto()             # &&
    BANG = auto()            # !
    NOT_EQUAL = auto()       # !=
    EQUAL = auto()           # ==
    GREATER_EQUAL = auto()   # >=
    LESS_EQUAL = auto()      # <=
    GREATER = auto()         # >
    LESS = auto()            # <
    LEFT_PAREN = auto()      # (
    RIGHT_PAREN = auto()     # )
    DOT = auto()             # .
    STRING = auto()          # 'literal'
    NUMBER = auto()          # 123 / 1.5
    IDENTIFIER = auto()      # name
    EOF = auto()


# Two-character operators are recognized before their single-character
# prefixes, mirroring the consumer's tokenizer.
_TWO_CHAR_TOKEN_TYPES = {
    '??': _TokenType.COALESCE,
    '||': _TokenType.OR,
    '&&': _TokenType.AND,
    '!=': _TokenType.NOT_EQUAL,
    '==': _TokenType.EQUAL,
    '>=': _TokenType.GREATER_EQUAL,
    '<=': _TokenType.LESS_EQUAL,
}

_ONE_CHAR_TOKEN_TYPES = {
    '?': _TokenType.QUESTION,
    ':': _TokenType.COLON,
    '!': _TokenType.BANG,
    '>': _TokenType.GREATER,
    '<': _TokenType.LESS,
    '(': _TokenType.LEFT_PAREN,
    ')': _TokenType.RIGHT_PAREN,
    '.': _TokenType.DOT,
}

# Structural punctuation is always tokenized; only the operator surface is
# gated by ALLOWED_EXPRESSION_OPERATORS.
_STRUCTURAL_CHARS = frozenset({'?', ':', '(', ')', '.'})


class _Token:
    """Lexer token: type, raw source text, absolute position and the parsed
    literal value (string/number tokens only)."""

    __slots__ = ('type', 'text', 'position', 'value')

    def __init__(
        self,
        token_type: _TokenType,
        text: str,
        position: int,
        value: Any = None,
    ) -> None:
        self.type = token_type
        self.text = text
        self.position = position
        self.value = value


def _is_digit(c: str) -> bool:
    """ASCII-digit check, matching the consumer's tokenizer (0-9 only)."""
    return '0' <= c <= '9'


def _is_identifier_start(c: str) -> bool:
    """Identifier head check: [A-Za-z_]."""
    return c == '_' or 'A' <= c <= 'Z' or 'a' <= c <= 'z'


def _is_identifier_part(c: str) -> bool:
    """Identifier tail check: [A-Za-z0-9_]."""
    return _is_identifier_start(c) or _is_digit(c)


def _tokenize(source: str, offset: int = 0) -> List[_Token]:
    """Tokenizes one token body into a token list ending with EOF.

    Whitespace is skipped; ``'...'`` literals terminate at the next quote
    (no escapes); numbers match ``\\d+(\\.\\d+)?`` — except a digit run
    right after a ``.`` token, which lexes as a bare integer because dots
    only exist inside paths (``a.b.0.1`` is ident + three index segments,
    never a ``0.1`` float literal). Two-character operators are recognized
    before their single-character prefixes. Anything else raises
    ``ValidationError``.

    Args:
        source: The expression source text (one ``{{...}}`` token body).
        offset: Absolute index of ``source`` inside the validated value, so
            reported positions point into the full value string.

    Returns:
        The token list, always terminated by an EOF token.

    Raises:
        ValidationError: On unterminated string literals or unexpected
            characters (message carries the absolute index).
    """
    tokens: List[_Token] = []
    i = 0
    length = len(source)
    while i < length:
        c = source[i]
        if c in _WHITESPACE:
            i += 1
            continue
        start = i
        pos = start + offset
        if c == "'":
            end = source.find("'", i + 1)
            if end < 0:
                raise ValidationError(f"unterminated string literal at index {pos}")
            text = source[i + 1:end]
            tokens.append(_Token(_TokenType.STRING, text, pos, text))
            i = end + 1
            continue
        if _is_digit(c):
            end = i + 1
            while end < length and _is_digit(source[end]):
                end += 1
            # A fractional part is consumed only when a digit follows the
            # dot — and only when the digit run does not itself follow a
            # '.' token: dots exist only inside paths, so 'a.b.0.1' lexes
            # as ident + integer segments, not 'a.b.' + a '0.1' float.
            after_dot = bool(tokens) and tokens[-1].type is _TokenType.DOT
            if (
                not after_dot
                and end + 1 < length
                and source[end] == '.'
                and _is_digit(source[end + 1])
            ):
                end += 2
                while end < length and _is_digit(source[end]):
                    end += 1
            text = source[i:end]
            value = float(text) if '.' in text else int(text)
            tokens.append(_Token(_TokenType.NUMBER, text, pos, value))
            i = end
            continue
        if _is_identifier_start(c):
            end = i + 1
            while end < length and _is_identifier_part(source[end]):
                end += 1
            tokens.append(_Token(_TokenType.IDENTIFIER, source[i:end], pos))
            i = end
            continue
        two = source[i:i + 2] if i + 1 < length else ''
        two_type = _TWO_CHAR_TOKEN_TYPES.get(two)
        if two_type is not None and two in ALLOWED_EXPRESSION_OPERATORS:
            tokens.append(_Token(two_type, two, pos))
            i += 2
            continue
        one_type = _ONE_CHAR_TOKEN_TYPES.get(c)
        if one_type is not None and (
            c in _STRUCTURAL_CHARS or c in ALLOWED_EXPRESSION_OPERATORS
        ):
            tokens.append(_Token(one_type, c, pos))
            i += 1
            continue
        raise ValidationError(f"unexpected character '{c}' at index {pos}")
    tokens.append(_Token(_TokenType.EOF, '', length + offset))
    return tokens


def _find_token_end(text: str, open_index: int) -> int:
    """Finds the end of the ``{{...}}`` token opened at ``open_index``.

    ``'...'`` string-literal regions are skipped so a ``}}`` inside quotes
    does not terminate the token — same scanning contract as the consumer.

    Args:
        text: The full value string.
        open_index: Index of the token's opening ``{{``.

    Returns:
        The index just past the closing ``}}``, or -1 when the token is
        never closed.
    """
    in_string = False
    i = open_index + 2
    while i < len(text) - 1:
        c = text[i]
        if in_string:
            if c == "'":
                in_string = False
        elif c == "'":
            in_string = True
        elif c == '}' and text[i + 1] == '}':
            return i + 2
        i += 1
    return -1


def _find_nested_open(inner: str) -> int:
    """Returns the index of a nested ``{{`` inside a token body, skipping
    ``'...'`` literals, or -1 when there is none."""
    in_string = False
    i = 0
    while i < len(inner) - 1:
        c = inner[i]
        if in_string:
            if c == "'":
                in_string = False
        elif c == "'":
            in_string = True
        elif c == '{' and inner[i + 1] == '{':
            return i
        i += 1
    return -1


MAX_EXPRESSION_DEPTH = 64
"""Maximum nesting depth accepted by ``_Parser``.

Bounds the recursive descent (ternaries, parentheses, ``!`` chains) so a
pathological source raises ``ValidationError`` instead of escaping the
plugin contract as ``RecursionError``.
"""


class _Parser:
    """Recursive-descent syntax checker for one token's source text.

    Validation only — the grammar is consumed for shape, never evaluated.
    Every failure raises ``ValidationError`` with the absolute source index
    of the offending token.
    """

    def __init__(self, tokens: List[_Token]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._depth = 0

    def _enter(self) -> None:
        """Counts one descent level; over the cap is a validation error."""
        self._depth += 1
        if self._depth > MAX_EXPRESSION_DEPTH:
            raise ValidationError(
                f"expression nesting exceeds {MAX_EXPRESSION_DEPTH} levels"
            )

    def _leave(self) -> None:
        self._depth -= 1

    @property
    def _current(self) -> _Token:
        """The token under the cursor."""
        return self._tokens[self._pos]

    def parse(self) -> None:
        """Entry point: a valid expression must consume every token."""
        self._parse_expression()
        if self._current.type is not _TokenType.EOF:
            raise ValidationError(
                f"unexpected token '{self._current.text}' "
                f"at index {self._current.position}"
            )

    def _match(self, token_type: _TokenType) -> bool:
        """Consumes the current token when it is of ``token_type``."""
        if self._current.type is not token_type:
            return False
        self._pos += 1
        return True

    def _expect(self, token_type: _TokenType, description: str) -> _Token:
        """Consumes the current token or raises a positional error."""
        if self._current.type is not token_type:
            raise ValidationError(
                f"expected {description}, found '{self._current.text}' "
                f"at index {self._current.position}"
            )
        token = self._current
        self._pos += 1
        return token

    # expr := nullCoalesce ('?' expr ':' expr)?
    def _parse_expression(self) -> None:
        self._enter()
        try:
            self._parse_null_coalesce()
            if not self._match(_TokenType.QUESTION):
                return
            self._parse_expression()
            self._expect(_TokenType.COLON, "':' in ternary expression")
            self._parse_expression()
        finally:
            self._leave()

    # nullCoalesce := orExpr ('??' orExpr)*
    def _parse_null_coalesce(self) -> None:
        self._parse_or()
        while self._match(_TokenType.COALESCE):
            self._parse_or()

    # orExpr := andExpr ('||' andExpr)*
    def _parse_or(self) -> None:
        self._parse_and()
        while self._match(_TokenType.OR):
            self._parse_and()

    # andExpr := equality ('&&' equality)*
    def _parse_and(self) -> None:
        self._parse_equality()
        while self._match(_TokenType.AND):
            self._parse_equality()

    # equality := comparison (('==' | '!=') comparison)*
    def _parse_equality(self) -> None:
        self._parse_comparison()
        while True:
            if self._match(_TokenType.EQUAL):
                self._parse_comparison()
            elif self._match(_TokenType.NOT_EQUAL):
                self._parse_comparison()
            else:
                return

    # comparison := unary (('>' | '<' | '>=' | '<=') unary)?
    # Non-associative by grammar: a second comparison operator is left
    # unconsumed and surfaces as an unexpected token at the top level.
    def _parse_comparison(self) -> None:
        self._parse_unary()
        if self._current.type in (
            _TokenType.GREATER,
            _TokenType.LESS,
            _TokenType.GREATER_EQUAL,
            _TokenType.LESS_EQUAL,
        ):
            self._pos += 1
            self._parse_unary()

    # unary := '!' unary | primary
    def _parse_unary(self) -> None:
        self._enter()
        try:
            if self._match(_TokenType.BANG):
                self._parse_unary()
                return
            self._parse_primary()
        finally:
            self._leave()

    # primary := literal | path | '(' expr ')'
    def _parse_primary(self) -> None:
        token = self._current
        if token.type in (_TokenType.STRING, _TokenType.NUMBER):
            self._pos += 1
            return
        if token.type is _TokenType.LEFT_PAREN:
            self._pos += 1
            self._parse_expression()
            self._expect(_TokenType.RIGHT_PAREN, "')'")
            return
        if token.type is _TokenType.IDENTIFIER:
            self._pos += 1
            if token.text in KEYWORD_LITERALS:
                # true / false / null are literals, not path heads.
                return
            self._parse_path()
            return
        raise ValidationError(
            f"expected expression, found '{token.text}' "
            f"at index {token.position}"
        )

    # path := ident ('.' segment)* — a segment is an identifier or a bare
    # integer so list indices ('items.0.name') resolve like a path.
    def _parse_path(self) -> None:
        while self._match(_TokenType.DOT):
            segment = self._current
            is_index = (
                segment.type is _TokenType.NUMBER
                and isinstance(segment.value, int)
            )
            if segment.type is not _TokenType.IDENTIFIER and not is_index:
                raise ValidationError(
                    f"expected identifier or list index after '.' "
                    f"at index {segment.position}"
                )
            self._pos += 1


def is_expression_string(value: Any) -> bool:
    """True when ``value`` is a string whose first non-whitespace content
    opens a closable ``{{...}}`` expression token — i.e. the value is
    expression-driven and its semantics resolve client-side.

    A ``{{`` opener that never closes is literal text (the consumer's
    scanner treats it the same way), so it does not qualify.

    Used by the strict-mode domain checks: an expression string carries
    dynamic content, so literal domain membership does not apply to it.
    The predicate is datatype-agnostic — it describes the *value's* shape,
    not the attribute's declared type (a `{{`-leading value on any
    domain-bound attribute is dynamic by contract).

    The predicate is shape-only — it does not check the token's grammar.
    Callers that skip a check because a value is expression-shaped must
    ensure the syntax is validated in another layer: the import pipeline's
    domain checks re-validate via ``validate_expression_value`` unless the
    attribute's datatype already gates it through its plugin validator.
    """
    if not isinstance(value, str):
        return False
    stripped = value.lstrip()
    if not stripped.startswith(EXPRESSION_OPEN):
        return False
    return _find_token_end(stripped, 0) != -1


def validate_expression_value(value: Any) -> None:
    """Validates that a value is a well-formed expression string.

    This is the registered datatype validator for the ``expression``
    datatype (see ``datatype_plugins.DATATYPE_VALIDATORS``). Per the plugin
    contract documented in ``datatype_plugins``, it raises
    ``django.core.exceptions.ValidationError`` for invalid input; any other
    exception type is treated as a bug in the validator.

    A valid value is either a non-string scalar literal (bool/int/float)
    or a string in which every ``{{ ... }}`` token is closed and parses
    completely against the expression grammar. Strings without tokens are
    plain literals and always valid.

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not a string/scalar, a ``{{``
            token is left unclosed or nested, a token body is empty, or a
            token body fails the grammar — messages carry the offending
            index inside the value string.
    """
    # Non-string scalars are degenerate literals the consumer coerces to
    # text; dicts/lists have no place in a string-typed property.
    if isinstance(value, (bool, int, float)):
        return
    if not isinstance(value, str):
        raise ValidationError(
            f"expression value must be a string or scalar literal, "
            f"got {type(value).__name__}"
        )

    # Scan '{{' tokens left to right: each must close and its body must
    # parse completely against the grammar.
    cursor = 0
    while True:
        open_index = value.find(EXPRESSION_OPEN, cursor)
        if open_index < 0:
            return
        end = _find_token_end(value, open_index)
        if end < 0:
            raise ValidationError(
                f"unclosed '{EXPRESSION_OPEN}' token at index {open_index}"
            )
        inner = value[open_index + 2:end - 2]
        _validate_token_body(inner, offset=open_index + 2)
        cursor = end


def _validate_token_body(inner: str, offset: int) -> None:
    """Validates one ``{{...}}`` token body against the expression grammar.

    Args:
        inner: The text between ``{{`` and ``}}``.
        offset: Absolute index of ``inner`` inside the full value string,
            used for positional error messages.

    Raises:
        ValidationError: On empty bodies, nested ``{{`` tokens or any
            grammar violation surfaced by the tokenizer/parser.
    """
    if not inner.strip():
        raise ValidationError(
            f"empty expression inside '{EXPRESSION_OPEN}{EXPRESSION_CLOSE}' "
            f"token at index {offset - 2}"
        )
    nested = _find_nested_open(inner)
    if nested >= 0:
        raise ValidationError(
            f"nested '{EXPRESSION_OPEN}' token inside expression "
            f"at index {offset + nested}"
        )
    _Parser(_tokenize(inner, offset)).parse()
