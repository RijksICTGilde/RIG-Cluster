#!/usr/bin/env python3
"""
Debug what the component extension generates for the failing button.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))


def debug_component_preprocessing():
    """Debug the preprocessing step to see what Jinja2 code is generated."""

    button_html = """<c-button
    kind="quaternary"
    size="sm"
    :showIcon="'before'"
    :icon="'verwijderen'"
    @click="removeComponentRow(this)">
    Verwijderen
</c-button>"""

    print("Original button HTML:")
    print(button_html)
    print("\n" + "=" * 50)

    try:
        from jinja2 import DictLoader, Environment
        from jinja_roos_components.extension import ComponentExtension

        # Create environment and extension
        env = Environment(loader=DictLoader({}))
        extension = ComponentExtension(env)

        # Use the preprocessor directly
        result = extension.preprocess(button_html, "test", None)

        print("Generated Jinja2 code:")
        print(result)
        print("\n" + "=" * 50)

        # Try to compile the generated code
        try:
            compiled = env.compile(result)
            print("✅ Generated code compiles successfully!")
        except Exception as compile_error:
            print(f"❌ Generated code fails to compile: {compile_error}")

            # Show line by line what was generated
            lines = result.split("\n")
            print("\nGenerated code line by line:")
            for i, line in enumerate(lines, 1):
                print(f"{i:3}: {line}")

    except Exception as e:
        print(f"❌ Preprocessing failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    print("Debugging Component Generation")
    print("=" * 50)

    debug_component_preprocessing()
