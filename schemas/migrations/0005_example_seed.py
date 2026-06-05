import json as _json
from django.db import migrations


# -----------------------------------------------------------------------------
# Example Seed: Survey System (Customer Satisfaction Survey)
#
# This demonstrates Dynamic Schema with a universal, real-world example that
# everyone understands: a customer satisfaction survey.
#
# Features demonstrated:
#   - Organization and Project structure
#   - Hierarchical composition (Survey → Sections → Questions)
#   - Multiple field types (text, email, number, choice, rating, date, boolean)
#   - Survey workflow (draft → published → closed)
#   - A fully-built example survey ready to use
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 1. Survey-specific domains
# -----------------------------------------------------------------------------
SURVEY_DOMAINS = [
    ('SurveyStatus',    'Workflow status for survey lifecycle'),
    ('QuestionType',    'Types of survey questions'),
    ('SatisfactionLevel','Customer satisfaction scale'),
    ('ServiceUsed',     'Services the customer has used'),
    ('ScoreRange',      'Labels for score_range int_tuple [min, max]'),
]

SURVEY_DOMAIN_ITEMS = {
    'SurveyStatus': [
        ('draft',     'Draft'),
        ('published', 'Published'),
        ('closed',    'Closed'),
    ],
    'QuestionType': [
        ('text',         'Short Text'),
        ('email',        'Email'),
        ('number',       'Number'),
        ('single_choice','Single Choice'),
        ('multiple_choice','Multiple Choice'),
        ('rating',       'Rating (1-5)'),
        ('textarea',     'Long Text'),
        ('date',         'Date'),
        ('boolean',      'Yes/No'),
    ],
    'SatisfactionLevel': [
        ('1', 'Very Dissatisfied'),
        ('2', 'Dissatisfied'),
        ('3', 'Neutral'),
        ('4', 'Satisfied'),
        ('5', 'Very Satisfied'),
    ],
    'ServiceUsed': [
        ('support',   'Customer Support'),
        ('delivery',  'Delivery Service'),
        ('website',   'Online Platform'),
        ('mobile_app','Mobile App'),
        ('in_store',  'In-Store Experience'),
    ],
    'ScoreRange': [
        ('min', 'Min score'),
        ('max', 'Max score'),
    ],
}

# -----------------------------------------------------------------------------
# 2. NodeTypes
# (name, label, is_container, is_root, json_scope)
# -----------------------------------------------------------------------------
SURVEY_NODE_TYPES = [
    # Root - the survey itself
    ('survey',              'Survey',           True,  True,  'survey_root'),
    
    # Sections group related questions
    ('survey_section',     'Survey Section',   True,  False, 'survey_section'),
    
    # Questions - the actual fields
    ('survey_question',    'Survey Question',  False, False, 'survey_question'),
]

# -----------------------------------------------------------------------------
# 3. NodeTypeCompositions
# Survey contains Sections, Sections contain Questions
# -----------------------------------------------------------------------------
SURVEY_COMPOSITIONS = [
    ('survey',          'survey_section',   'sections',  1, None),
    ('survey_section',  'survey_question',  'questions', 1, None),
]

# -----------------------------------------------------------------------------
# 4. Universal AttributeDefs
# -----------------------------------------------------------------------------
SURVEY_UNIVERSAL_ATTR_DEFS = [
    # Survey root attributes
    ('survey',           'id',             'natural_uuid', None,           True),
    ('survey',           'title',          'string',       None,           True),
    ('survey',           'description',    'string',       None,           False),
    ('survey',           'status',         'string',       'SurveyStatus', True),
    ('survey',           'anonymous',      'bool',         None,           False),
    ('survey',           'start_date',     'date',         None,           False),
    ('survey',           'end_date',       'date',         None,           False),
    ('survey',           'response_count', 'int',          None,           False),
    # data_type showcase
    ('survey',           'brand_color',    'color',        None,           False),
    ('survey',           'tags',           'list_string',  None,           False),
    ('survey',           'allowed_scores', 'list_int',     None,           False),
    ('survey',           'score_range',    'int_tuple',    'ScoreRange',   False),
    ('survey',           'meta',           'dict',         None,           False),
    
    # Section attributes
    ('survey_section',   'id',             'natural_uuid', None,           True),
    ('survey_section',   'title',          'string',       None,           True),
    ('survey_section',   'description',    'string',       None,           False),
    ('survey_section',   'sort_order',     'display_order',None,           False),

    # Question attributes — universal (apply to ALL question types)
    ('survey_question',  'id',             'natural_uuid', None,           True),
    ('survey_question',  'label',          'string',       None,           True),
    ('survey_question',  'type',           'string',       'QuestionType', True),
    ('survey_question',  'required',       'bool',         None,           False),
    ('survey_question',  'placeholder',    'string',       None,           False),
    ('survey_question',  'help_text',      'string',       None,           False),
    ('survey_question',  'sort_order',     'display_order',None,           False),
]

# -----------------------------------------------------------------------------
# 5. NodeTypeVariants + variant-scoped AttributeDefs for survey_question
#
# Each variant represents a question type with its exclusive properties.
# Universal attrs (label, type, required, etc.) are NOT repeated here.
# -----------------------------------------------------------------------------
QUESTION_VARIANT_ATTR_DEFS = {
    'text': [
        # (json_key, data_type_name, domain_name_or_None, is_required)
        ('max_length',  'int',    None, False),
    ],
    'email': [
        ('max_length',  'int',    None, False),
    ],
    'number': [
        ('min_value',   'int',    None, False),
        ('max_value',   'int',    None, False),
    ],
    'single_choice': [
        ('options',     'list_string', None, True),
    ],
    'multiple_choice': [
        ('options',     'list_string', None, True),
        ('max_selections', 'int', None, False),
    ],
    'rating': [
        ('min_value',   'int',    None, False),
        ('max_value',   'int',    None, False),
    ],
    'textarea': [
        ('max_length',  'int',    None, False),
        ('min_lines',   'int',    None, False),
        ('max_lines',   'int',    None, False),
    ],
    'date': [
        ('min_date',    'date',   None, False),
        ('max_date',    'date',   None, False),
    ],
    'boolean': [
        ('true_label',  'string', None, False),
        ('false_label', 'string', None, False),
    ],
}


def create_survey_structure(apps, schema_editor):
    """
    Create the survey schema structure (domains, node types, compositions, attributes).
    """
    DataType = apps.get_model('schemas', 'DataType')
    Domain = apps.get_model('schemas', 'Domain')
    DomainItem = apps.get_model('schemas', 'DomainItem')
    NodeType = apps.get_model('schemas', 'NodeType')
    NodeTypeComposition = apps.get_model('schemas', 'NodeTypeComposition')
    AttributeDef = apps.get_model('schemas', 'AttributeDef')

    # -- Domains
    domain_by_name = {}
    for domain_name, description in SURVEY_DOMAINS:
        obj, _ = Domain.objects.get_or_create(
            domain_name=domain_name,
            defaults={'description': description},
        )
        domain_by_name[domain_name] = obj

    for domain_name, items in SURVEY_DOMAIN_ITEMS.items():
        domain_obj = domain_by_name[domain_name]
        for value, label in items:
            DomainItem.objects.get_or_create(
                domain=domain_obj, value=value, defaults={'label': label}
            )

    # -- NodeTypes
    nt_by_name = {}
    for name, label, is_container, is_root, json_scope in SURVEY_NODE_TYPES:
        nt, _ = NodeType.objects.get_or_create(
            name=name,
            defaults={
                'label': label,
                'is_container': is_container,
                'is_root': is_root,
                'json_scope': json_scope,
            },
        )
        nt_by_name[name] = nt

    # -- Compositions
    for parent_name, child_name, collection_key, min_c, max_c in SURVEY_COMPOSITIONS:
        if child_name not in nt_by_name:
            continue
        NodeTypeComposition.objects.get_or_create(
            parent_type=nt_by_name[parent_name],
            child_type=nt_by_name[child_name],
            defaults={
                'collection_key': collection_key,
                'min_children': min_c,
                'max_children': max_c,
            },
        )

    # -- DataTypes lookup
    dt_by_name = {dt.name: dt for dt in DataType.objects.all()}

    # -- Universal AttributeDefs (variant_key=NULL, is_common=True)
    seen_universal = set()
    for node_type_name, json_key, dt_name, domain_name, is_required in SURVEY_UNIVERSAL_ATTR_DEFS:
        key = (node_type_name, json_key)
        if key in seen_universal:
            continue
        seen_universal.add(key)
        AttributeDef.objects.get_or_create(
            node_type=nt_by_name[node_type_name],
            json_key=json_key,
            variant_key=None,
            defaults={
                'name': json_key,
                'is_required': is_required,
                'is_common': True,
                'data_type': dt_by_name[dt_name],
                'domain': domain_by_name.get(domain_name) if domain_name else None,
            },
        )

    # -- NodeTypeVariants + variant-scoped AttributeDefs for survey_question
    NodeTypeVariant = apps.get_model('schemas', 'NodeTypeVariant')
    question_nt = nt_by_name['survey_question']

    # Create catalog AttributeDefs (variant_key=NULL, is_common=False)
    # Catalog entries are templates — never required (only variant copies can be required)
    seen_catalog = set()
    for variant_key, attr_defs in QUESTION_VARIANT_ATTR_DEFS.items():
        for json_key, dt_name, domain_name, is_required in attr_defs:
            if json_key not in seen_catalog:
                seen_catalog.add(json_key)
                AttributeDef.objects.get_or_create(
                    node_type=question_nt,
                    json_key=json_key,
                    variant_key=None,
                    defaults={
                        'name': json_key,
                        'is_required': False,
                        'is_common': False,
                        'data_type': dt_by_name[dt_name],
                        'domain': domain_by_name.get(domain_name) if domain_name else None,
                    },
                )

    # Create NodeTypeVariants and variant-specific AttributeDefs
    for variant_key, attr_defs in QUESTION_VARIANT_ATTR_DEFS.items():
        NodeTypeVariant.objects.get_or_create(
            node_type=question_nt,
            variant_key=variant_key,
        )
        for json_key, dt_name, domain_name, is_required in attr_defs:
            AttributeDef.objects.get_or_create(
                node_type=question_nt,
                json_key=json_key,
                variant_key=variant_key,
                defaults={
                    'name': json_key,
                    'is_required': is_required,
                    'is_common': False,
                    'data_type': dt_by_name[dt_name],
                    'domain': domain_by_name.get(domain_name) if domain_name else None,
                },
            )


def _set_node_attr(NodeAttribute, attr_def, node, value):
    """Set a single attribute value on a node, dispatching by data type."""
    dt = attr_def.data_type.name
    if dt in ('natural_uuid', 'natural_key', 'natural_version', 'natural_order', 'display_order'):
        return
    if dt == 'bool':
        NodeAttribute.objects.create(node=node, attribute_def=attr_def, value_bool=value)
    elif dt in ('int', 'float', 'number'):
        NodeAttribute.objects.create(node=node, attribute_def=attr_def, value_number=value)
    elif dt in ('list_string', 'list_int', 'int_tuple', 'dict', 'json', 'domain_list'):
        v = value if not isinstance(value, str) else _json.loads(value)
        NodeAttribute.objects.create(node=node, attribute_def=attr_def, value_json=v)
    else:
        NodeAttribute.objects.create(node=node, attribute_def=attr_def, value_string=str(value))


def _set_section_attrs(NodeAttribute, AttributeDef, nt_section, section_node, attrs):
    """Set attributes on a survey_section node."""
    for json_key, value in attrs.items():
        attr_def = AttributeDef.objects.get(node_type=nt_section, json_key=json_key)
        _set_node_attr(NodeAttribute, attr_def, section_node, value)


def _create_question(Node, NodeAttribute, AttributeDef, nt_question, project, parent, q_data):
    """Create a survey_question node with its attributes.

    Resolves AttributeDefs correctly for variant-scoped attributes:
    - Universal attrs (is_common=True, variant_key=NULL): label, type, required, etc.
    - Variant-scoped attrs (variant_key=<type>): options, min_value, max_value, etc.
    """
    variant_key = q_data.get('type')
    # Convert 1-based display_order to 0-based sort_order for Node.sort_order
    sort_order = q_data['sort_order'] - 1 if 'sort_order' in q_data else 0
    question = Node.objects.create(
        node_type=nt_question,
        project=project,
        parent=parent,
        sort_order=sort_order,
        name=q_data['label'],
    )
    for json_key, value in q_data.items():
        # Try universal (is_common=True) first, then variant-scoped
        attr_def = AttributeDef.objects.filter(
            node_type=nt_question, json_key=json_key, variant_key=None, is_common=True
        ).first()
        if not attr_def:
            attr_def = AttributeDef.objects.filter(
                node_type=nt_question, json_key=json_key, variant_key=variant_key
            ).first()
        if attr_def:
            _set_node_attr(NodeAttribute, attr_def, question, value)


def create_example_survey(apps, schema_editor):
    """
    Create a fully-built example survey with sample organization and project.
    
    This demonstrates how to build a complete survey with:
    - Organization: "Demo Corp"
    - Project: "Customer Experience"
    - Survey: "Customer Satisfaction Survey 2026"
    - Multiple sections and question types
    """
    Organization = apps.get_model('schemas', 'Organization')
    Project = apps.get_model('schemas', 'Project')
    Node = apps.get_model('schemas', 'Node')
    NodeAttribute = apps.get_model('schemas', 'NodeAttribute')
    NodeType = apps.get_model('schemas', 'NodeType')
    AttributeDef = apps.get_model('schemas', 'AttributeDef')
    
    # -- Create Organization
    org, _ = Organization.objects.get_or_create(
        slug='demo-corp',
        defaults={
            'name': 'Demo Corp',
            'description': 'Example organization for demonstration purposes',
            'is_active': True,
        }
    )
    
    # -- Create Project
    project, _ = Project.objects.get_or_create(
        slug='customer-experience',
        defaults={
            'name': 'Customer Experience',
            'description': 'Customer feedback and satisfaction initiatives',
            'organization': org,
        }
    )
    
    # -- Get NodeTypes
    nt_survey = NodeType.objects.get(name='survey')
    nt_section = NodeType.objects.get(name='survey_section')
    nt_question = NodeType.objects.get(name='survey_question')
    
    # -- Create Survey Node
    survey = Node.objects.create(
        node_type=nt_survey,
        project=project,
        key='customer-satisfaction-2026',
        version='1',
        name='Customer Satisfaction Survey 2026',
    )
    
    # Set survey attributes (natural_version is not stored in NodeAttribute)
    survey_attrs = {
        'title': 'Customer Satisfaction Survey 2026',
        'description': 'Help us improve by sharing your experience',
        'status': 'draft',
        'anonymous': True,
        'response_count': 0,
    }
    # Note: version is stored natively in schema_nodes.version, not as a NodeAttribute
    
    for json_key, value in survey_attrs.items():
        attr_def = AttributeDef.objects.get(node_type=nt_survey, json_key=json_key)
        if attr_def.data_type.name == 'bool':
            NodeAttribute.objects.create(
                node=survey, attribute_def=attr_def, value_bool=value
            )
        elif attr_def.data_type.name == 'int':
            NodeAttribute.objects.create(
                node=survey, attribute_def=attr_def, value_number=value
            )
        else:
            NodeAttribute.objects.create(
                node=survey, attribute_def=attr_def, value_string=str(value)
            )
    
    # -- Create Section 1: About You
    section1 = Node.objects.create(
        node_type=nt_section,
        project=project,
        parent=survey,
        sort_order=0,
        name='Section 1: About You',
    )

    # Section 1 attributes
    _set_section_attrs(NodeAttribute, AttributeDef, nt_section, section1, {
        'title': 'About You',
        'description': 'Basic information',
        'sort_order': 1,  # display_order value (1-based for humans)
    })
    
    # Section 1 Questions
    section1_questions = [
        {
            'label': 'What is your name?',
            'type': 'text',
            'required': True,
            'placeholder': 'John Doe',
            'sort_order': 1,
        },
        {
            'label': 'Email address',
            'type': 'email',
            'required': True,
            'placeholder': 'john@example.com',
            'help_text': 'We will never share your email',
            'sort_order': 2,
        },
        {
            'label': 'Your age',
            'type': 'number',
            'required': False,
            'placeholder': '30',
            'min_value': 18,
            'max_value': 100,
            'sort_order': 3,
        },
    ]
    
    for q_data in section1_questions:
        _create_question(Node, NodeAttribute, AttributeDef, nt_question, project, section1, q_data)
    
    # -- Create Section 2: Your Experience
    section2 = Node.objects.create(
        node_type=nt_section,
        project=project,
        parent=survey,
        sort_order=1,
        name='Section 2: Your Experience',
    )

    _set_section_attrs(NodeAttribute, AttributeDef, nt_section, section2, {
        'title': 'Your Experience',
        'description': 'Rate our services',
        'sort_order': 2,  # display_order value (1-based for humans)
    })
    
    # Section 2 Questions
    section2_questions = [
        {
            'label': 'Which services have you used?',
            'type': 'multiple_choice',
            'required': True,
            'options': ['support', 'delivery', 'website', 'mobile_app', 'in_store'],
            'help_text': 'Select all that apply',
            'sort_order': 1,
        },
        {
            'label': 'How satisfied are you with our service?',
            'type': 'rating',
            'required': True,
            'min_value': 1,
            'max_value': 5,
            'sort_order': 2,
        },
        {
            'label': 'Would you recommend us to a friend?',
            'type': 'boolean',
            'required': True,
            'sort_order': 3,
        },
        {
            'label': 'When did you last visit?',
            'type': 'date',
            'required': False,
            'sort_order': 4,
        },
        {
            'label': 'Any additional comments?',
            'type': 'textarea',
            'required': False,
            'placeholder': 'Tell us more...',
            'sort_order': 5,
        },
    ]
    
    for q_data in section2_questions:
        _create_question(Node, NodeAttribute, AttributeDef, nt_question, project, section2, q_data)


def seed_survey_example(apps, schema_editor):
    """
    Seed the complete Survey System example.
    
    This creates:
    1. Survey schema structure (domains, node types, attributes)
    2. A fully-built example survey with organization and project
    3. Multiple question types demonstrating the system's flexibility
    """
    create_survey_structure(apps, schema_editor)
    create_example_survey(apps, schema_editor)


def remove_survey_example(apps, schema_editor):
    """Remove all Survey example data (reverse migration)"""
    from django.db.models import Q
    
    Node = apps.get_model('schemas', 'Node')
    NodeAttribute = apps.get_model('schemas', 'NodeAttribute')
    NodeType = apps.get_model('schemas', 'NodeType')
    NodeTypeVariant = apps.get_model('schemas', 'NodeTypeVariant')
    AttributeDef = apps.get_model('schemas', 'AttributeDef')
    Domain = apps.get_model('schemas', 'Domain')
    Organization = apps.get_model('schemas', 'Organization')
    Project = apps.get_model('schemas', 'Project')
    
    # Delete nodes first (to avoid FK constraints)
    survey_nt_names = [nt[0] for nt in SURVEY_NODE_TYPES]
    survey_nts = NodeType.objects.filter(name__in=survey_nt_names)
    
    # Delete nodes belonging to these node types or with survey key
    survey_nodes = Node.objects.filter(
        Q(node_type__in=survey_nts) | Q(key='customer-satisfaction-2026')
    )
    NodeAttribute.objects.filter(node__in=survey_nodes).delete()
    survey_nodes.delete()
    
    # Delete org and project
    Project.objects.filter(slug='customer-experience').delete()
    Organization.objects.filter(slug='demo-corp').delete()
    
    # Delete attribute definitions and variants
    for nt in survey_nts:
        AttributeDef.objects.filter(node_type=nt).delete()
        NodeTypeVariant.objects.filter(node_type=nt).delete()
    
    # Delete node types and domains
    survey_nts.delete()
    Domain.objects.filter(domain_name__in=[d[0] for d in SURVEY_DOMAINS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0004_base_seed"),
    ]

    operations = [
        migrations.RunPython(seed_survey_example, reverse_code=remove_survey_example),
    ]
