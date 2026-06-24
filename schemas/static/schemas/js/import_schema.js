// Toggle between text and file input modes
(function () {
    const modeText = document.getElementById('mode_text');
    const modeFile = document.getElementById('mode_file');
    const textInput = document.getElementById('json-text-input');
    const fileInput = document.getElementById('json-file-input');
    const fileEl = document.getElementById('id_schema_file');
    const preview = document.getElementById('json-file-preview');
    const textarea = document.getElementById('id_schema_text');

    function applyMode(mode) {
        if (mode === 'file') {
            textInput.style.display = 'none';
            fileInput.style.display = 'block';
        } else {
            textInput.style.display = 'block';
            fileInput.style.display = 'none';
        }
    }

    modeText.addEventListener('change', function () { applyMode('text'); });
    modeFile.addEventListener('change', function () { applyMode('file'); });

    fileEl.addEventListener('change', function () {
        const file = this.files[0];
        if (!file) {
            preview.style.display = 'none';
            textarea.value = '';
            return;
        }
        const reader = new FileReader();
        reader.onload = function (e) {
            let content = e.target.result;
            try {
                content = JSON.stringify(JSON.parse(content), null, 2);
            } catch (_) {}
            textarea.value = content;
            preview.textContent = content.length > 2000 ? content.slice(0, 2000) + '\n…' : content;
            preview.style.display = 'block';
        };
        reader.readAsText(file, 'UTF-8');
    });
}());

// Show JSON example when schema type is selected
document.getElementById('id_schema_type').addEventListener('change', function() {
    const selectedOption = this.options[this.selectedIndex];
    const exampleKey = selectedOption.getAttribute('data-example-key');
    const exampleRow = document.getElementById('json-example-row');
    const examplePre = document.getElementById('json-example');
    
    if (exampleKey && window.jsonExampleUrl) {
        // Load example dynamically from DB using the URL from template
        fetch(`${window.jsonExampleUrl}?schema_type=${exampleKey}`)
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                if (data.example) {
                    examplePre.textContent = JSON.stringify(data.example, null, 2);
                    exampleRow.style.display = 'block';
                } else {
                    exampleRow.style.display = 'none';
                }
            })
            .catch(error => {
                console.error('Error loading JSON example:', error);
                exampleRow.style.display = 'none';
            });
    } else {
        exampleRow.style.display = 'none';
    }
});

document.getElementById('import-schema-form').addEventListener('submit', function(e) {
    e.preventDefault();

    const errorDiv = document.getElementById('error-message');
    errorDiv.style.display = 'none';

    const mode = document.querySelector('input[name="json_input_mode"]:checked').value;
    const textarea = document.getElementById('id_schema_text');
    if (mode === 'file' && !textarea.value.trim()) {
        errorDiv.textContent = 'Error: Debes seleccionar un archivo JSON antes de importar.';
        errorDiv.style.display = 'block';
        return;
    }

    const formData = new FormData(this);
    const changelistUrl = document.getElementById('import-schema-form').dataset.changelistUrl;
    
    fetch('', {
        method: 'POST',
        body: formData,
        headers: {
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => {
        if (response.ok) {
            return response.json();
        } else {
            return response.text().then(text => {
                try {
                    return JSON.parse(text);
                } catch (e) {
                    // If response is HTML, extract error message or show generic error
                    return { error: 'Server returned HTML instead of JSON', detail: 'Status: ' + response.status };
                }
            });
        }
    })
    .then(data => {
        if (data && data.error) {
            errorDiv.textContent = 'Error: ' + data.error;
            if (data.detail) {
                errorDiv.textContent += ' - ' + data.detail;
            }
            errorDiv.style.display = 'block';
        } else if (data && data.success) {
            // Show warning if present
            if (data.warning) {
                errorDiv.textContent = 'Warning: ' + data.warning.message;
                errorDiv.style.background = '#fffbe6'; // Yellow background for warning
                errorDiv.style.borderColor = '#ffe58f';
                errorDiv.style.display = 'block';
            }
            // Redirect to changelist after a short delay to show the warning
            setTimeout(() => {
                window.location.href = changelistUrl;
            }, 2000);
        }
    })
    .catch(error => {
        errorDiv.textContent = 'Error: ' + error.message;
        errorDiv.style.display = 'block';
    });
});
