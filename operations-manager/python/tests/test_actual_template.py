#!/usr/bin/env python3
"""
Test the actual self-service-portal.html.j2 template to reproduce the real error.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))


def test_actual_template():
    """Test the actual template that's failing."""

    print("Testing the actual self-service-portal.html.j2 template...")

    try:
        from opi.core.templates import get_templates

        templates = get_templates()

        # Try to load and render the actual template
        template = templates.get_template("self-service-portal.html.j2")

        # Create minimal context (we don't need to render fully, just parse)
        context = {
            "request": type("Request", (), {"url": type("URL", (), {"path": "/test"})()})(),
            "menu_items": [],
            "projects": [],
            "services": [],
            "templates": [],
        }

        result = template.render(context)
        print("✅ Template loaded and rendered successfully!")
        print(f"Rendered length: {len(result)} characters")

        return True

    except Exception as e:
        print("❌ Template failed to render")
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")

        # Check if this is the specific error we're looking for
        error_str = str(e)
        if "expected token" in error_str and "got ':'" in error_str:
            print("🎯 This IS the error we're investigating!")
        else:
            print("🤔 This is a different error")

        import traceback

        traceback.print_exc()
        return False


def test_template_loading_only():
    """Just test template loading without rendering."""

    print("\nTesting template loading only (no rendering)...")

    try:
        from opi.core.templates import get_templates

        templates = get_templates()

        # Just load the template (this triggers parsing)
        template = templates.get_template("self-service-portal.html.j2")
        print("✅ Template loaded successfully (parsing worked)")

        return True

    except Exception as e:
        print("❌ Template loading failed")
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")

        # Check if this is the specific error
        error_str = str(e)
        if "expected token" in error_str and "got ':'" in error_str:
            print("🎯 This IS the error we're investigating!")
        else:
            print("🤔 This is a different error")

        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("Testing Actual Template Error")
    print("=" * 50)

    # First test just loading the template
    load_success = test_template_loading_only()

    if load_success:
        # If loading works, try rendering
        render_success = test_actual_template()

        if render_success:
            print("\n🎉 No errors found! The button syntax works fine.")
        else:
            print("\n🔍 Error occurs during rendering, not parsing.")
    else:
        print("\n🔍 Error occurs during template loading/parsing.")
