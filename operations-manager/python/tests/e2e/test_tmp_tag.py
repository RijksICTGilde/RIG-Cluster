import pytest
pytestmark = pytest.mark.e2e

def test_tag(app_server, page):
    page.goto(f"{app_server}/lotc/")
    page.wait_for_load_state("networkidle")
    page.evaluate("""() => {
      const h = document.querySelector('.lotc-app-shell__main') || document.body;
      h.innerHTML = '<nldd-tag id="t1" color="success" text="MetTekst"></nldd-tag>'
        + '<nldd-tag id="t2" color="success"><span style="width:8px;height:8px;background:red;display:inline-block"></span>Inhoud</nldd-tag>';
    }""")
    page.wait_for_timeout(800)
    print("T1:", repr(page.locator("#t1").inner_text()))
    print("T2:", repr(page.locator("#t2").inner_text()))
    print("T2 dot visible:", page.locator("#t2 span").count() and page.locator("#t2 span").first.is_visible())
