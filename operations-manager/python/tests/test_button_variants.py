#!/usr/bin/env python3
"""Test script to verify all button variants, sizes, and states."""

import sys
from pathlib import Path

# Add jinja-roos-components to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import ComponentExtension

# Setup paths
component_templates = (
    Path(__file__).parent.parent.parent / "jinja-roos-components" / "jinja_roos_components" / "templates"
)

# Create test template with all button variants
test_template = """
<!DOCTYPE html>
<html>
<head>
    <title>Button Variants Test</title>
    <style>
        body { font-family: sans-serif; padding: 20px; }
        .section { margin-bottom: 30px; }
        .section h2 { margin-bottom: 15px; }
        .button-row { display: flex; gap: 10px; margin-bottom: 10px; align-items: center; }
        .label { min-width: 150px; }
    </style>
</head>
<body>
    <h1>RVO Button Component Test</h1>
    
    <div class="section">
        <h2>Button Types (kind)</h2>
        
        <div class="button-row">
            <span class="label">Primary:</span>
            <c-button kind="primary">Primary Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Secondary:</span>
            <c-button kind="secondary">Secondary Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Tertiary:</span>
            <c-button kind="tertiary">Tertiary Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Quaternary:</span>
            <c-button kind="quaternary">Quaternary Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Subtle:</span>
            <c-button kind="subtle">Subtle Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Warning:</span>
            <c-button kind="warning">Warning Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Warning Subtle:</span>
            <c-button kind="warning-subtle">Warning Subtle</c-button>
        </div>
    </div>
    
    <div class="section">
        <h2>Button Sizes</h2>
        
        <div class="button-row">
            <span class="label">Extra Small (xs):</span>
            <c-button kind="primary" size="xs">XS Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Small (sm):</span>
            <c-button kind="primary" size="sm">Small Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Medium (md):</span>
            <c-button kind="primary" size="md">Medium Button</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Default (md):</span>
            <c-button kind="primary">Default Size</c-button>
        </div>
    </div>
    
    <div class="section">
        <h2>Button States</h2>
        
        <div class="button-row">
            <span class="label">Normal:</span>
            <c-button kind="primary">Normal State</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Hover:</span>
            <c-button kind="primary" :hover="true">Hover State</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Focus:</span>
            <c-button kind="primary" :focus="true">Focus State</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Active:</span>
            <c-button kind="primary" :active="true">Active State</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Disabled:</span>
            <c-button kind="primary" :disabled="true">Disabled State</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Busy:</span>
            <c-button kind="primary" :busy="true">Loading...</c-button>
        </div>
    </div>
    
    <div class="section">
        <h2>Button with Icons</h2>
        
        <div class="button-row">
            <span class="label">Icon Before:</span>
            <c-button kind="primary" :showIcon="'before'" :icon="'plus'">Add Item</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Icon After:</span>
            <c-button kind="primary" :showIcon="'after'" :icon="'pijl-naar-rechts'">Next</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">Tertiary + Icon:</span>
            <c-button kind="tertiary" size="sm" :showIcon="'before'" :icon="'plus'">
                Gebruiker toevoegen
            </c-button>
        </div>
    </div>
    
    <div class="section">
        <h2>Special Cases</h2>
        
        <div class="button-row">
            <span class="label">Full Width:</span>
            <div style="width: 400px;">
                <c-button kind="primary" :fullWidth="true">Full Width Button</c-button>
            </div>
        </div>
        
        <div class="button-row">
            <span class="label">Submit Type:</span>
            <c-button kind="primary" type="submit">Submit Form</c-button>
        </div>
        
        <div class="button-row">
            <span class="label">With Event:</span>
            <c-button kind="secondary" @click="alert('Clicked!')">Click Me</c-button>
        </div>
    </div>
    
    <div class="section">
        <h2>Combined Attributes</h2>
        
        <div class="button-row">
            <span class="label">Complex Button:</span>
            <c-button 
                kind="warning"
                size="sm"
                :showIcon="'before'"
                :icon="'waarschuwing'"
                @click="console.log('Warning clicked')"
                data-action="delete"
                aria-label="Delete item">
                Delete
            </c-button>
        </div>
    </div>
</body>
</html>
"""

# Create environment
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

# Render template
template = env.from_string(test_template)
html_output = template.render()

# Save output
output_file = Path(__file__).parent / "test_button_variants.html"
output_file.write_text(html_output)

print("✓ Template rendered successfully")
print(f"✓ Output written to: {output_file}")

# Verify button types are correctly mapped
checks = [
    # Primary and secondary should have appearance attribute
    ('data-utrecht-button-appearance="primary-action-button"', "Primary button appearance"),
    ('data-utrecht-button-appearance="secondary-action-button"', "Secondary button appearance"),
    # Tertiary and quaternary should have specific classes
    ("utrecht-button--rvo-tertiary-action", "Tertiary button class"),
    ("utrecht-button--rvo-quaternary-action", "Quaternary button class"),
    # Warning should have hint attribute
    ('data-utrecht-button-hint="warning"', "Warning button hint"),
    # Subtle should have appearance
    ('data-utrecht-button-appearance="subtle-button"', "Subtle button appearance"),
    # Size classes
    ("utrecht-button--rvo-xs", "XS size class"),
    ("utrecht-button--rvo-sm", "SM size class"),
    ("utrecht-button--rvo-md", "MD size class"),
    # State classes
    ("utrecht-button--hover", "Hover state class"),
    ("utrecht-button--focus", "Focus state class"),
    ("utrecht-button--active", "Active state class"),
    ("utrecht-button--busy", "Busy state class"),
    # Icon classes
    ("rvo-icon-plus", "Plus icon"),
    ("rvo-icon-pijl-naar-rechts", "Arrow icon"),
    ("rvo-icon-waarschuwing", "Warning icon"),
    ("rvo-icon-spinner", "Spinner icon for busy state"),
    # Full width
    ("utrecht-button--rvo-full-width", "Full width class"),
    # Icon position classes
    ("utrecht-button--icon-before", "Icon before class"),
    ("utrecht-button--icon-after", "Icon after class"),
    # Event handlers
    ('onclick="alert(&#39;Clicked!&#39;)"', "Click event handler"),
    # Data attributes
    ('data-action="delete"', "Data attribute"),
    ('aria-label="Delete item"', "ARIA label"),
]

print("\nVerifying button implementations:")
all_passed = True
for expected, description in checks:
    if expected in html_output:
        print(f"  ✓ {description}")
    else:
        print(f"  ✗ {description} - NOT FOUND")
        all_passed = False

# Count button instances
button_count = html_output.count("<button")
print(f"\n📊 Total buttons rendered: {button_count}")

# Check for the specific tertiary button with icon
if "Gebruiker toevoegen" in html_output:
    # Find the button context
    start = html_output.find("Gebruiker toevoegen") - 500
    end = html_output.find("Gebruiker toevoegen") + 100
    button_context = html_output[start:end]

    if "utrecht-button--rvo-tertiary-action" in button_context:
        print("✓ 'Gebruiker toevoegen' correctly uses tertiary style")
    else:
        print("✗ 'Gebruiker toevoegen' NOT using tertiary style")
        all_passed = False

if all_passed:
    print("\n✅ All button variant tests passed!")
    print(f"\n🌐 Open {output_file} in a browser to see the visual result")
    sys.exit(0)
else:
    print("\n❌ Some tests failed")
    sys.exit(1)
