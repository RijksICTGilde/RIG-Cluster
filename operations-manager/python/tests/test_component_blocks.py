#!/usr/bin/env python3
"""
Test to verify component processing within Jinja2 blocks.
"""

from jinja2 import DictLoader, Environment
from jinja_roos_components import setup_components

# Test templates
templates = {
    "base.html.j2": """
<!DOCTYPE html>
<html>
<head><title>Test</title></head>
<body>
    {% block content %}{% endblock %}
</body>
</html>
""",
    "child.html.j2": """
{% extends "base.html.j2" %}
{% block content %}
    <div>
        <c-heading type="h2" textContent="Test Heading"/>
        <c-layout-flow gap="lg">
            <c-card padding="md">
                <p>Test content</p>
            </c-card>
        </c-layout-flow>
    </div>
{% endblock %}
""",
    "direct.html.j2": """
<div>
    <c-heading type="h2" textContent="Direct Test"/>
    <c-layout-flow gap="lg">
        <c-card padding="md">
            <p>Direct content</p>
        </c-card>
    </c-layout-flow>
</div>
""",
}

# Create environment with our test templates
env = Environment(loader=DictLoader(templates))
setup_components(env)

print("Testing direct template (no inheritance):")
print("=" * 50)
direct_template = env.get_template("direct.html.j2")
direct_output = direct_template.render()
print(direct_output)
print(
    "\nDirect template has unprocessed components:",
    "c-heading" in direct_output or "c-layout-flow" in direct_output or "c-card" in direct_output,
)

print("\n\nTesting child template (with inheritance):")
print("=" * 50)
child_template = env.get_template("child.html.j2")
child_output = child_template.render()
print(child_output)
print(
    "\nChild template has unprocessed components:",
    "c-heading" in child_output or "c-layout-flow" in child_output or "c-card" in child_output,
)
