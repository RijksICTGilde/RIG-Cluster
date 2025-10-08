#!/usr/bin/env python3
"""
Simple test case to isolate the button parsing issue.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))

from jinja2 import DictLoader, Environment


def test_button_parsing_directly():
    """Test the exact button syntax that's failing."""

    # The exact button code that's failing from line 356
    button_html = """<c-button
    kind="quaternary"
    size="sm"
    :showIcon="'before'"
    :icon="'verwijderen'"
    @click="removeComponentRow(this)">
    Verwijderen
</c-button>"""

    print("Testing button HTML:")
    print(button_html)
    print("\n" + "=" * 50)

    try:
        # Test 1: Try with the operations manager's exact template setup
        print("Test 1: Operations Manager Template Setup")
        from opi.core.templates import get_templates

        templates = get_templates()

        # Create a simple test template
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html.j2", delete=False) as f:
            f.write(f"""<!DOCTYPE html>
<html>
<body>
{button_html}
</body>
</html>""")
            temp_path = f.name

        # Try to render it
        template = templates.env.from_string(open(temp_path).read())
        result = template.render()
        print("✅ Operations Manager setup: SUCCESS")
        print("Rendered length:", len(result))

        os.unlink(temp_path)

    except Exception as e:
        print("❌ Operations Manager setup: FAILED")
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")
        import traceback

        traceback.print_exc()
        print()

    try:
        # Test 2: Try with basic jinja setup + components
        print("Test 2: Basic Jinja + ROOS Components")
        from jinja_roos_components import setup_components

        env = Environment(
            loader=DictLoader(
                {
                    "test.html": f"""<!DOCTYPE html>
<html>
<body>
{button_html}
</body>
</html>"""
                }
            )
        )

        setup_components(env, strict_validation=False)
        template = env.get_template("test.html")
        result = template.render()
        print("✅ Basic setup: SUCCESS")
        print("Rendered length:", len(result))

    except Exception as e:
        print("❌ Basic setup: FAILED")
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")
        import traceback

        traceback.print_exc()
        print()

    try:
        # Test 3: Try with DOM-based setup
        print("Test 3: DOM-based Setup")
        from jinja_roos_components import setup_components_dom

        env = Environment(
            loader=DictLoader(
                {
                    "test.html": f"""<!DOCTYPE html>
<html>
<body>
{button_html}
</body>
</html>"""
                }
            )
        )

        setup_components_dom(env)
        template = env.get_template("test.html")
        result = template.render()
        print("✅ DOM setup: SUCCESS")
        print("Rendered length:", len(result))

    except Exception as e:
        print("❌ DOM setup: FAILED")
        print(f"Error: {e}")
        print(f"Error type: {type(e).__name__}")
        import traceback

        traceback.print_exc()
        print()


def test_simplified_variations():
    """Test variations to isolate the exact issue."""

    variations = [
        ("Without colon attributes", """<c-button kind="quaternary" size="sm">Verwijderen</c-button>"""),
        ("With one colon attribute", """<c-button kind="quaternary" :showIcon="'before'">Verwijderen</c-button>"""),
        ("With event handler", """<c-button kind="quaternary" @click="test()">Verwijderen</c-button>"""),
        ("Colon + event", """<c-button :showIcon="'before'" @click="test()">Verwijderen</c-button>"""),
        (
            "Full original",
            """<c-button kind="quaternary" size="sm" :showIcon="'before'" :icon="'verwijderen'" @click="removeComponentRow(this)">Verwijderen</c-button>""",
        ),
    ]

    from jinja2 import DictLoader, Environment
    from jinja_roos_components import setup_components

    env = Environment(loader=DictLoader({}))
    setup_components(env, strict_validation=False)

    print("Testing variations:")
    print("=" * 50)

    for description, html in variations:
        try:
            template = env.from_string(html)
            result = template.render()
            print(f"✅ {description}: SUCCESS")
        except Exception as e:
            print(f"❌ {description}: FAILED - {e}")


if __name__ == "__main__":
    print("Button Parsing Issue Test")
    print("=" * 50)

    test_button_parsing_directly()
    print()
    test_simplified_variations()
