document.getElementById('import-schema-form').addEventListener('submit', function(e) {
    e.preventDefault();
    
    const formData = new FormData(this);
    const errorDiv = document.getElementById('error-message');
    const changelistUrl = document.getElementById('import-schema-form').dataset.changelistUrl;
    errorDiv.style.display = 'none';
    
    fetch('', {
        method: 'POST',
        body: formData,
        headers: {
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => {
        if (response.ok) {
            window.location.href = changelistUrl;
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
        }
    })
    .catch(error => {
        errorDiv.textContent = 'Error: ' + error.message;
        errorDiv.style.display = 'block';
    });
});
