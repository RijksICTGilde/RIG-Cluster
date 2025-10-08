// JavaScript functions for managing components in the self-service portal

let componentCounter = 1;

function addComponentRow() {
    componentCounter++;
    const componentsList = document.getElementById('components-list');
    
    const newComponent = document.createElement('div');
    newComponent.className = 'component-item rvo-card rvo-card--outline rvo-card--padding-lg';
    newComponent.innerHTML = `
        <c-layout-flow gap="md">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <c-heading type="h3" textContent="Component ${componentCounter}" />
                <c-button 
                    kind="quaternary"
                    size="sm" 
                    :showIcon="'before'" 
                    :icon="'verwijderen'"
                    @click="removeComponentRow(this)">
                    Verwijderen
                </c-button>
            </div>

            <div class="rvo-layout-row rvo-layout-gap--md">
                <div class="rvo-layout-column rvo-layout-column--md-6">
                    <c-select-field
                        id="component-type-${componentCounter}"
                        name="components[${componentCounter - 1}][type]"
                        label="Component Type"
                        :options="[
                            {'label': 'Single (All-in-one)', 'value': 'single'},
                            {'label': 'Frontend', 'value': 'frontend'},
                            {'label': 'Backend', 'value': 'backend'}
                        ]"
                        :value="'single'" />
                </div>
                <div class="rvo-layout-column rvo-layout-column--md-6">
                    <c-text-input-field
                        id="component-port-${componentCounter}"
                        name="components[${componentCounter - 1}][port]"
                        label="Poort"
                        type="number"
                        placeholder="8080"
                        helperText="Poort waarop de applicatie draait" />
                </div>
            </div>

            <c-text-input-field
                id="component-image-${componentCounter}"
                name="components[${componentCounter - 1}][image]"
                label="Container Image"
                placeholder="registry.example.com/my-app:latest"
                helperText="Docker image van uw applicatie. Moet een rootless image zijn (draait niet als root gebruiker)."
                expandableHelperText="true"
                expandableHelperTextTitle="Meer info over rootless images" />

            <div class="rvo-layout-row rvo-layout-gap--md">
                <div class="rvo-layout-column rvo-layout-column--md-6">
                    <c-select-field
                        id="component-cpu-${componentCounter}"
                        name="components[${componentCounter - 1}][cpu_limit]"
                        label="CPU Limiet"
                        :options="[
                            {'label': '1 CPU', 'value': '1'},
                            {'label': '2 CPU', 'value': '2'},
                            {'label': '3 CPU', 'value': '3'},
                            {'label': '4 CPU', 'value': '4'}
                        ]"
                        :value="'1'"
                        helperText="Aantal CPU cores toegewezen aan dit component"
                        :expandableHelperText="true"
                        expandableHelperTextTitle="Meer info over CPU limieten" />
                </div>
                <div class="rvo-layout-column rvo-layout-column--md-6">
                    <c-select-field
                        id="component-memory-${componentCounter}"
                        name="components[${componentCounter - 1}][memory_limit]"
                        label="Memory Limiet"
                        :options="[
                            {'label': '128 MB', 'value': '128Mi'},
                            {'label': '256 MB', 'value': '256Mi'},
                            {'label': '512 MB', 'value': '512Mi'},
                            {'label': '768 MB', 'value': '768Mi'},
                            {'label': '1 GB', 'value': '1Gi'}
                        ]"
                        :value="'256Mi'"
                        helperText="Maximum RAM geheugen beschikbaar voor dit component"
                        :expandableHelperText="true"
                        expandableHelperTextTitle="Meer info over memory limieten" />
                </div>
            </div>

            <div class="service-binding">
                <c-heading type="h4" textContent="Gekoppelde Services" />
                <p class="rvo-text--sm">Selecteer welke services dit component mag gebruiken:</p>
                
                <c-layout-row gap="md">
                    <c-layout-column>
                        <c-card variant="outline" padding="sm">
                            <c-layout-row gap="sm" alignItems="center">
                                <c-icon name="waarschuwing" size="md" color="oranje" />
                                <c-layout-column style="flex: 1;">
                                    <div style="font-weight: 600;">Keycloak</div>
                                    <div class="rvo-text--xs" style="color: var(--rvo-color-grijs-700);">Identity Management</div>
                                </c-layout-column>
                                <c-checkbox 
                                    id="component-${componentCounter}-service-keycloak"
                                    name="components[${componentCounter - 1}][services][]"
                                    value="keycloak" />
                            </c-layout-row>
                        </c-card>
                    </c-layout-column>

                    <c-layout-column>
                        <c-card variant="outline" padding="sm">
                            <c-layout-row gap="sm" alignItems="center">
                                <c-icon name="database" size="md" color="blauw" />
                                <c-layout-column style="flex: 1;">
                                    <div style="font-weight: 600;">PostgreSQL</div>
                                    <div class="rvo-text--xs" style="color: var(--rvo-color-grijs-700);">Database</div>
                                </c-layout-column>
                                <c-checkbox 
                                    id="component-${componentCounter}-service-postgres"
                                    name="components[${componentCounter - 1}][services][]"
                                    value="postgresql" />
                            </c-layout-row>
                        </c-card>
                    </c-layout-column>

                    <c-layout-column>
                        <c-card variant="outline" padding="sm">
                            <c-layout-row gap="sm" alignItems="center">
                                <c-icon name="map" size="md" color="rood" />
                                <c-layout-column style="flex: 1;">
                                    <div style="font-weight: 600;">MinIO</div>
                                    <div class="rvo-text--xs" style="color: var(--rvo-color-grijs-700);">Object Storage</div>
                                </c-layout-column>
                                <c-checkbox 
                                    id="component-${componentCounter}-service-minio"
                                    name="components[${componentCounter - 1}][services][]"
                                    value="minio" />
                            </c-layout-row>
                        </c-card>
                    </c-layout-column>
                </c-layout-row>
            </div>
        </c-layout-flow>
    `;
    
    componentsList.appendChild(newComponent);
}

function removeComponentRow(button) {
    const componentItem = button.closest('.component-item');
    if (componentItem) {
        // Only remove if there's more than one component
        const componentsList = document.getElementById('components-list');
        const componentItems = componentsList.querySelectorAll('.component-item');
        
        if (componentItems.length > 1) {
            componentItem.remove();
            // Renumber remaining components
            updateComponentNumbers();
        } else {
            alert('Er moet ten minste één component blijven bestaan.');
        }
    }
}

function updateComponentNumbers() {
    const componentsList = document.getElementById('components-list');
    const componentItems = componentsList.querySelectorAll('.component-item');
    
    componentItems.forEach((item, index) => {
        // Update heading
        const heading = item.querySelector('c-heading');
        if (heading) {
            heading.setAttribute('textContent', `Component ${index + 1}`);
        }
        
        // Update form field names and IDs
        const fields = item.querySelectorAll('[name], [id]');
        fields.forEach(field => {
            if (field.hasAttribute('name')) {
                const name = field.getAttribute('name');
                const newName = name.replace(/components\[\d+\]/, `components[${index}]`);
                field.setAttribute('name', newName);
            }
            if (field.hasAttribute('id')) {
                const id = field.getAttribute('id');
                const newId = id.replace(/-\d+(-|$)/, `-${index + 1}$1`);
                field.setAttribute('id', newId);
            }
        });
    });
}

// Additional helper functions for user management (existing)
function addUserRow() {
    // Implementation for adding user rows
    console.log('Adding user row...');
}

function removeUserRow(button) {
    const userRow = button.closest('.user-row');
    if (userRow) {
        const usersList = document.getElementById('users-list');
        const userRows = usersList ? usersList.querySelectorAll('.user-row') : [];
        
        if (userRows.length > 1) {
            userRow.remove();
        } else {
            alert('Er moet ten minste één gebruiker blijven bestaan.');
        }
    }
}