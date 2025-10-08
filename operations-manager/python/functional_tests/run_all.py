#!/usr/bin/env python3
"""
Runner script for all functional tests.
"""

import asyncio
import os
import sys

# Add the parent directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from functional_tests.test_argocd_application_creation import TestArgocdApplicationCreation


async def run_all_tests():
    """Run all functional tests."""
    print("🚀 Starting All Functional Tests\n")

    tests_passed = 0
    tests_failed = 0

    # Test 1: ArgoCD Application Creation
    print("=" * 60)
    print("TEST 1: ArgoCD Application Creation")
    print("=" * 60)

    try:
        test = TestArgocdApplicationCreation()

        # Run connectivity test first
        connectivity_ok = await test.run_git_connectivity_test()
        print()

        # Run main test
        main_test_ok = await test.test_argocd_application_creation()

        if main_test_ok:
            tests_passed += 1
            print("✅ TEST 1 PASSED\n")
        else:
            tests_failed += 1
            print("❌ TEST 1 FAILED\n")

    except Exception as e:
        tests_failed += 1
        print(f"❌ TEST 1 FAILED with exception: {e}\n")

    # Future tests can be added here...

    # Final summary
    total_tests = tests_passed + tests_failed
    print("=" * 60)
    print("FINAL RESULTS")
    print("=" * 60)
    print(f"Total Tests: {total_tests}")
    print(f"Passed: {tests_passed}")
    print(f"Failed: {tests_failed}")

    if tests_failed == 0:
        print("\n🎉 ALL FUNCTIONAL TESTS PASSED!")
        return True
    else:
        print(f"\n💥 {tests_failed} FUNCTIONAL TEST(S) FAILED!")
        return False


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
