/**
 * Global Domain Manager
 * Handles the Floating Action Button (FAB) and domain fetching/display logic
 * for Cloudflare and Namecheap across all pages.
 */

// Make functions globally accessible
window.toggleDomainFab = function() {
    const options = document.getElementById('domain-fab-options');
    const icon = document.querySelector('.domain-fab-main i');
    
    if (options.classList.contains('active')) {
        options.classList.remove('active');
        icon.classList.remove('fa-times');
        icon.classList.add('fa-globe');
    } else {
        options.classList.add('active');
        icon.classList.remove('fa-globe');
        icon.classList.add('fa-times');
    }
}

// Close FAB when clicking outside
document.addEventListener('click', function(event) {
    const fabContainer = document.querySelector('.domain-fab-container');
    const options = document.getElementById('domain-fab-options');
    const icon = document.querySelector('.domain-fab-main i');
    
    if (fabContainer && !fabContainer.contains(event.target)) {
        if (options && options.classList.contains('active')) {
            options.classList.remove('active');
            if (icon) {
                icon.classList.remove('fa-times');
                icon.classList.add('fa-globe');
            }
        }
    }
});

/* ==========================================
   CLOUDFLARE FUNCTIONS
   ========================================== */

window.openCloudflareDomainsModal = function() {
    fetchCloudflareDomains();
}

window.fetchCloudflareDomains = function() {
    const modal = document.getElementById('cloudflareDomainsModal');
    const loading = document.getElementById('cloudflare-domains-loading');
    const content = document.getElementById('cloudflare-domains-content');
    const errorDiv = document.getElementById('cloudflare-domains-error');

    if (modal) modal.style.display = 'block';
    if (loading) loading.style.display = 'block';
    if (content) content.style.display = 'none';
    if (errorDiv) errorDiv.style.display = 'none';

    fetch('/api/cloudflare-domains')
        .then(response => response.json())
        .then(data => {
            if (loading) loading.style.display = 'none';

            if (data.success) {
                if (content) content.style.display = 'block';
                const countElem = document.getElementById('cloudflare-domains-count');
                if (countElem) countElem.textContent = `Found ${data.total} domains`;

                const tbody = document.getElementById('cloudflare-domains-list');
                if (tbody) {
                    tbody.innerHTML = '';

                    if (data.domains && data.domains.length > 0) {
                        data.domains.forEach(domain => {
                            const row = document.createElement('tr');
                            row.style.borderBottom = '1px solid var(--border-subtle)';
                            row.innerHTML = `
                            <td style="padding: 6px 10px; font-family: monospace; color: var(--text-primary); font-size: 13px;">${domain.name}</td>
                            <td style="padding: 6px 10px;">
                                <span style="background: ${domain.status === 'active' ? 'rgba(46, 160, 67, 0.15)' : 'rgba(110, 118, 129, 0.15)'}; 
                                             color: ${domain.status === 'active' ? '#3fb950' : '#8b949e'}; 
                                             padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600;">
                                    ${domain.status}
                                </span>
                            </td>
                            <td style="padding: 6px 10px; font-family: monospace; font-size: 11px; color: var(--text-muted);">${domain.id}</td>
                        `;
                            tbody.appendChild(row);
                        });
                    } else {
                        tbody.innerHTML = '<tr><td colspan="3" style="padding: 20px; text-align: center; color: var(--text-muted);">No domains found in Cloudflare.</td></tr>';
                    }
                }
            } else {
                if (errorDiv) {
                    errorDiv.style.display = 'block';
                    errorDiv.innerHTML = `
                    <strong>Error fetching Cloudflare domains:</strong><br>
                    ${data.error}
                `;
                }
            }
        })
        .catch(error => {
            if (loading) loading.style.display = 'none';
            if (errorDiv) {
                errorDiv.style.display = 'block';
                errorDiv.textContent = 'Network error: ' + error.message;
            }
        });
}

window.closeCloudflareDomainsModal = function() {
    const modal = document.getElementById('cloudflareDomainsModal');
    if (modal) modal.style.display = 'none';
}

window.copyCloudflareDomains = function() {
    const rows = document.querySelectorAll('#cloudflare-domains-list tr');
    let domains = [];
    rows.forEach(row => {
        const domain = row.cells[0].textContent;
        if (domain) domains.push(domain);
    });

    if (domains.length > 0) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(domains.join('\n'))
                .then(() => showStatus('✅ Domains copied to clipboard', 'success'))
                .catch(err => showStatus('❌ Failed to copy: ' + err, 'error'));
        } else {
            // Fallback for non-secure contexts
            const textArea = document.createElement("textarea");
            textArea.value = domains.join('\n');
            document.body.appendChild(textArea);
            textArea.select();
            try {
                document.execCommand('copy');
                showStatus('✅ Domains copied to clipboard', 'success');
            } catch (err) {
                showStatus('❌ Failed to copy', 'error');
            }
            document.body.removeChild(textArea);
        }
    }
}


/* ==========================================
   NAMECHEAP FUNCTIONS
   ========================================== */

window.openNamecheapDomainsModal = function() {
    // We reuse the proceedWithFetch logic but simplified for global context
    const modal = document.getElementById('namecheapDomainsModal');
    const loading = document.getElementById('namecheap-domains-loading');
    const content = document.getElementById('namecheap-domains-content');
    const errorDiv = document.getElementById('namecheap-domains-error');

    if (modal) modal.style.display = 'flex'; // Flex for centering usually
    if (loading) loading.style.display = 'block';
    if (content) content.style.display = 'none';
    if (errorDiv) errorDiv.style.display = 'none';

    fetch('/api/namecheap-domains', {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' }
    })
        .then(response => {
            if (!response.ok) {
                return response.json().then(data => {
                    throw new Error(data.error || `HTTP ${response.status}: ${response.statusText}`);
                });
            }
            return response.json();
        })
        .then(data => {
            if (data.success) {
                displayNamecheapDomains(data.domains);
            } else {
                let errorMsg = data.error || 'Failed to fetch domains';
                if (data.debug_info) errorMsg += '\n\n' + data.debug_info;
                showNamecheapDomainsError(errorMsg, data);
            }
        })
        .catch(error => {
            console.error('Error fetching Namecheap domains:', error);
            showNamecheapDomainsError('Network error: ' + error);
        });
}

window.displayNamecheapDomains = function(domains) {
    const loading = document.getElementById('namecheap-domains-loading');
    const content = document.getElementById('namecheap-domains-content');
    const error = document.getElementById('namecheap-domains-error');
    const list = document.getElementById('namecheap-domains-list');
    const count = document.getElementById('namecheap-domains-count');

    if (loading) loading.style.display = 'none';
    if (error) error.style.display = 'none';
    if (content) content.style.display = 'block';

    if (count) count.textContent = domains.length;

    if (list) {
        if (domains.length === 0) {
            list.innerHTML = '<p style="text-align: center; color: var(--text-muted); padding: 20px;">No domains found in your Namecheap account.</p>';
            return;
        }

        let html = '';
        domains.forEach((domain, index) => {
            const domainName = domain.name || domain;
            html += `
            <div class="domain-item-row" style="padding: 6px 10px; border-bottom: 1px solid var(--border-subtle); cursor: pointer; transition: background-color 0.2s; display: flex; justify-content: space-between; align-items: center;"
                 onmouseover="this.style.backgroundColor='var(--bg-panel)'"
                 onmouseout="this.style.backgroundColor='transparent'"
                 onclick="copyDomainToClipboard('${domainName}')"
                 title="Click to copy">
                <span class="domain-name-text" style="color: var(--accent-fg); font-weight: 500; font-size: 13px;">${domainName}</span>
                ${domain.expire_date ? `<span style="color: var(--text-muted); font-size: 11px;">Expires: ${domain.expire_date}</span>` : ''}
            </div>
        `;
        });
        list.innerHTML = html;
    }
}

window.showNamecheapDomainsError = function(message, debugData) {
    const loading = document.getElementById('namecheap-domains-loading');
    const content = document.getElementById('namecheap-domains-content');
    const error = document.getElementById('namecheap-domains-error');
    const errorMsg = document.getElementById('namecheap-domains-error-message');
    // We intentionally ignore complex debug steps here for the quick modal to keep it clean, 
    // unless strictly necessary.

    if (loading) loading.style.display = 'none';
    if (content) content.style.display = 'none';
    if (error) error.style.display = 'block';

    if (errorMsg) errorMsg.innerHTML = message.replace(/\n/g, '<br>');
    
    // Also show global toast
    if (typeof showStatus === 'function') {
        showStatus('❌ ' + message.split('\n')[0], 'error');
    }
}

window.closeNamecheapDomainsModal = function() {
    const modal = document.getElementById('namecheapDomainsModal');
    if (modal) modal.style.display = 'none';
}

window.copyAllNamecheapDomains = function() {
    const listItems = document.querySelectorAll('#namecheap-domains-list > div');
    const domains = [];
    listItems.forEach(item => {
        const domainSpan = item.querySelector('span'); // First span is domain name
        if (domainSpan) domains.push(domainSpan.textContent.trim());
    });

    if (domains.length === 0) {
        if (typeof showStatus === 'function') showStatus('No domains to copy', 'error');
        return;
    }

    const domainsText = domains.join('\n');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(domainsText)
            .then(() => { if (typeof showStatus === 'function') showStatus('✅ Copied all Namecheap domains', 'success'); })
            .catch(err => { if (typeof showStatus === 'function') showStatus('❌ Copy failed: ' + err, 'error'); });
    } else {
        // Fallback
        const textArea = document.createElement("textarea");
        textArea.value = domainsText;
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            if (typeof showStatus === 'function') showStatus('✅ Copied all domains', 'success');
        } catch (err) {
            console.error(err);
        }
        document.body.removeChild(textArea);
    }
}

window.copyDomainToClipboard = function(domain) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(domain)
            .then(() => { if (typeof showStatus === 'function') showStatus(`✅ Copied: ${domain}`, 'success'); })
            .catch(err => console.error(err));
    } else {
         const textArea = document.createElement("textarea");
        textArea.value = domain;
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
             if (typeof showStatus === 'function') showStatus(`✅ Copied: ${domain}`, 'success');
        } catch (err) { }
        document.body.removeChild(textArea);
    }
}

/* ==========================================
   SEARCH / FILTER FUNCTIONS
   ========================================== */

window.filterNamecheapDomains = function() {
    const input = document.getElementById('namecheap-search');
    const filter = input.value.toUpperCase();
    const list = document.getElementById('namecheap-domains-list');
    const items = list.getElementsByClassName('domain-item-row');
    
    for (let i = 0; i < items.length; i++) {
        const span = items[i].querySelector('.domain-name-text');
        if (span) {
            const txtValue = span.textContent || span.innerText;
            if (txtValue.toUpperCase().indexOf(filter) > -1) {
                items[i].style.display = "flex";
            } else {
                items[i].style.display = "none";
            }
        }
    }
}

window.filterCloudflareDomains = function() {
    const input = document.getElementById('cloudflare-search');
    const filter = input.value.toUpperCase();
    const list = document.getElementById('cloudflare-domains-list');
    const rows = list.getElementsByTagName('tr');
    
    for (let i = 0; i < rows.length; i++) {
        // Search in first column (Domain name)
        const td = rows[i].getElementsByTagName('td')[0];
        if (td) {
            const txtValue = td.textContent || td.innerText;
            if (txtValue.toUpperCase().indexOf(filter) > -1) {
                rows[i].style.display = "";
            } else {
                rows[i].style.display = "none";
            }
        }
    }
}

// Global click handler to close modals
window.onclick = function(event) {
    const cfModal = document.getElementById('cloudflareDomainsModal');
    const ncModal = document.getElementById('namecheapDomainsModal');
    
    if (cfModal && event.target === cfModal) {
        cfModal.style.display = "none";
    }
    if (ncModal && event.target === ncModal) {
        ncModal.style.display = "none";
    }
}
