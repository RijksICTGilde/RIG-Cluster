"""
Debug why nested components aren't being processed
"""

import re

# The regex pattern from the extension
pattern = re.compile(r"<c-([\w-]+)([^>]*?)(?:/>|>(.*?)</c-\1>)", re.DOTALL | re.MULTILINE)

# Test with the actual template content snippet
test_content = """<c-page title="Project Aanmaken - ROOS" lang="nl">
    <c-layout-flow gap="xl">
        <!-- Header -->
        <c-card background="color" backgroundColor="hemelblauw" padding="lg">
            <h1 class="utrecht-heading-1" style="color: white; margin: 0;">Project Aanmaken - ROOS</h1>
            <p style="color: rgba(255,255,255,0.9); margin-top: 0.5rem;">Simple ROOS component demonstration</p>
        </c-card>

        <!-- Form -->
        <c-card outline="true" padding="lg">
            <form method="POST" action="/api/projects/create-basic">
                <c-layout-flow gap="lg">
                    <div>content</div>
                </c-layout-flow>
            </form>
        </c-card>
    </c-layout-flow>
</c-page>"""

print("Testing regex pattern matching...")
print(f"Pattern: {pattern.pattern}\n")

# Find all matches
matches = list(pattern.finditer(test_content))
print(f"Found {len(matches)} matches:\n")

for i, match in enumerate(matches):
    print(f"Match {i+1}:")
    print(f"  Full match: {match.group(0)[:60]!r}...")
    print(f"  Component: {match.group(1)}")
    print(f"  Start: {match.start()}, End: {match.end()}")
    print()

# Test what happens if we process just the c-page content
page_match = pattern.search(test_content)
if page_match:
    print("\nProcessing c-page match:")
    print(f"Component: {page_match.group(1)}")
    print(f"Content length: {len(page_match.group(3)) if page_match.group(3) else 0}")

    # Now search within the content
    inner_content = page_match.group(3)
    inner_matches = list(pattern.finditer(inner_content))
    print(f"Found {len(inner_matches)} matches inside c-page content")

print("\n--- Testing simpler cases ---")

# Test simple nested case
simple = "<c-card><c-button>Test</c-button></c-card>"
simple_matches = list(pattern.finditer(simple))
print(f"Simple nested: {simple}")
print(f"Matches: {len(simple_matches)}")
for m in simple_matches:
    print(f"  - {m.group(1)}: {m.group(0)}")
