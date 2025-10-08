#!/usr/bin/env python3
"""
Test the c-select-field that might be causing the real issue.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../jinja-roos-components"))


def test_select_field():
    """Test the c-select-field with complex :options attribute."""

    # Get more context around the select field
    select_html = """<c-select-field
                                                                id="component-type-1"
                                                                name="components[0][type]"
                                                                label="Component Type"
                                                                :options="[
                                                                        {'label': 'Single (All-in-one)', 'value': 'single'},
                                                                        {'label': 'Frontend', 'value': 'frontend'},
                                                                        {'label': 'Backend', 'value': 'backend'}
                                                                ]"/>"""

    print("Testing c-select-field:")
    print(select_html)
    print("\n" + "=" * 50)

    try:
        from jinja2 import DictLoader, Environment
        from jinja_roos_components.extension import ComponentExtension

        env = Environment(loader=DictLoader({}))
        extension = ComponentExtension(env)

        result = extension.preprocess(select_html, "test", None)

        print("Generated Jinja2 code:")
        print(result)
        print("\n" + "=" * 50)

        try:
            compiled = env.compile(result)
            print("✅ c-select-field compiles successfully!")
        except Exception as compile_error:
            print(f"❌ c-select-field fails to compile: {compile_error}")
            print("This might be the real issue!")

            # Show the problematic generated code
            lines = result.split("\n")
            print("\nGenerated code line by line:")
            for i, line in enumerate(lines, 1):
                print(f"{i:3}: {line}")

    except Exception as e:
        print(f"❌ Processing failed: {e}")
        print("This is likely the real issue!")
        import traceback

        traceback.print_exc()


def test_larger_context():
    """Test with more context around line 356."""

    # Let's test a larger chunk of the template
    larger_context = """                                                        <c-button
                                                            kind="quaternary"
                                                            size="sm"
                                                            :showIcon="'before'"
                                                            :icon="'verwijderen'"
                                                            @click="removeComponentRow(this)">
                                                            Verwijderen
                                                        </c-button>
                                                    </div>

                                                    <c-layout-row gap="md">
                                                        <c-layout-column size="md-6">
                                                            <c-select-field
                                                                id="component-type-1"
                                                                name="components[0][type]"
                                                                label="Component Type"
                                                                :options="[
                                                                        {'label': 'Single (All-in-one)', 'value': 'single'},
                                                                        {'label': 'Frontend', 'value': 'frontend'},
                                                                        {'label': 'Backend', 'value': 'backend'}
                                                                ]"
                                                                :value="'single'"/>"""

    print("\nTesting larger context including select field:")
    print(larger_context)
    print("\n" + "=" * 50)

    try:
        from jinja2 import DictLoader, Environment
        from jinja_roos_components import setup_components

        env = Environment(loader=DictLoader({"test.html": larger_context}))
        setup_components(env, strict_validation=False)

        template = env.get_template("test.html")
        print("✅ Larger context loads successfully!")

    except Exception as e:
        print(f"❌ Larger context fails: {e}")
        if "expected token" in str(e) and "got ':'" in str(e):
            print("🎯 Found the same error in larger context!")


if __name__ == "__main__":
    print("Testing c-select-field Component")
    print("=" * 50)

    test_select_field()
    test_larger_context()
