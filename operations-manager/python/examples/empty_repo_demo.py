#!/usr/bin/env python3
"""
Demo script showing how the GitConnector handles empty repositories.
"""

import asyncio
import os
import sys
import tempfile

# Add the parent directory to Python path so we can import from opi
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opi.connectors.git import create_git_connector


async def demo_empty_repo_handling():
    """Demonstrate empty repository handling."""
    print("=== Empty Repository Handling Demo ===\n")

    # Test URLs - you can modify these for your setup
    test_scenarios = [
        {
            "name": "Empty SSH Repository",
            "url": "ssh://git@localhost:2222/srv/git/empty-test.git",
            "branch": "main",
            "ssh_key": "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keys/git-server-key",
        },
        {
            "name": "Empty SSH Repository with Custom Branch",
            "url": "ssh://git@localhost:2222/srv/git/empty-test.git",
            "branch": "develop",
            "ssh_key": "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keys/git-server-key",
        },
    ]

    for scenario in test_scenarios:
        print(f"Testing: {scenario['name']}")
        print(f"URL: {scenario['url']}")
        print(f"Branch: {scenario['branch']}")

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # Create connector
                connector = create_git_connector(
                    repo_url=scenario["url"],
                    working_dir=temp_dir,
                    branch=scenario["branch"],
                    ssh_key_path=scenario.get("ssh_key"),
                )

                # Try to clone the repository
                print("  → Attempting to clone...")
                await connector.ensure_repo_cloned()
                print(f"  ✓ Successfully cloned and set up branch '{scenario['branch']}'")

                # Test creating a file and committing
                test_file = os.path.join(temp_dir, "test.txt")
                with open(test_file, "w") as f:
                    f.write(f"Test content for branch {scenario['branch']}")

                # Add and commit the file
                add_cmd = ["add", "test.txt"]
                commit_cmd = ["commit", "-m", f"Add test file to {scenario['branch']}"]

                _, _, code = await connector._run_git_command(add_cmd)
                if code == 0:
                    _, _, code = await connector._run_git_command(commit_cmd)
                    if code == 0:
                        print("  ✓ Successfully added and committed test file")
                    else:
                        print("  ⚠ Failed to commit test file")
                else:
                    print("  ⚠ Failed to add test file")

                # Clean up
                await connector.close()

            except Exception as e:
                print(f"  ✗ Error: {e}")

        print()


if __name__ == "__main__":
    asyncio.run(demo_empty_repo_handling())
