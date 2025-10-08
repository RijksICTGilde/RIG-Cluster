#!/usr/bin/env python3
"""
Test with the exact context and indentation from the real template.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))


def test_exact_context():
    """Test with the exact context from the template."""

    # Extract more context around line 356
    template_snippet = """                                                        <c-button
                                                            kind="quaternary"
                                                            size="sm"
                                                            :showIcon="'before'"
                                                            :icon="'verwijderen'"
                                                            @click="removeComponentRow(this)">
                                                            Verwijderen
                                                        </c-button>"""

    print("Testing with exact template context:")
    print(template_snippet)
    print("\n" + "=" * 50)

    try:
        from jinja2 import DictLoader, Environment
        from jinja_roos_components.extension import ComponentExtension

        env = Environment(loader=DictLoader({}))
        extension = ComponentExtension(env)

        # Preprocess
        result = extension.preprocess(template_snippet, "test", None)

        print("Generated Jinja2 code:")
        print(repr(result))  # Use repr to see exact characters
        print("\nFormatted:")
        print(result)
        print("\n" + "=" * 50)

        # Try to compile
        try:
            compiled = env.compile(result)
            print("✅ Exact context compiles successfully!")
        except Exception as compile_error:
            print(f"❌ Exact context fails to compile: {compile_error}")

    except Exception as e:
        print(f"❌ Processing failed: {e}")
        import traceback

        traceback.print_exc()


def test_with_minimal_surrounding():
    """Test with some minimal surrounding template structure."""

    template_with_context = """<div>
                                                        <c-button
                                                            kind="quaternary"
                                                            size="sm"
                                                            :showIcon="'before'"
                                                            :icon="'verwijderen'"
                                                            @click="removeComponentRow(this)">
                                                            Verwijderen
                                                        </c-button>
                                                    </div>"""

    print("\nTesting with minimal surrounding context:")
    print(template_with_context)
    print("\n" + "=" * 50)

    try:
        from jinja2 import DictLoader, Environment
        from jinja_roos_components import setup_components

        env = Environment(loader=DictLoader({"test.html": template_with_context}))
        setup_components(env, strict_validation=False)

        template = env.get_template("test.html")
        print("✅ Template with context loads successfully!")

    except Exception as e:
        print(f"❌ Template with context fails: {e}")
        if "expected token" in str(e):
            print("🎯 Found the same error!")


if __name__ == "__main__":
    print("Testing Exact Context")
    print("=" * 50)

    test_exact_context()
    test_with_minimal_surrounding()
