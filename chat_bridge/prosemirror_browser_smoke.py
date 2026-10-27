"""Optional offline browser fixture for the saved ChatGPT 2026-10-11 ProseMirror form.

No network or chat page access. This is a synthetic composer shape extracted from
an operator-provided HTML save, without any conversation body or private data.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

SOURCE = Path(__file__).with_name("github_first_composer.js").read_text()
HTML = '''<!doctype html><html><body>
<style>.ProseMirror{min-height:50px}</style><form data-chatgpt-composer="" data-composer-placement="home" onsubmit="event.preventDefault()">
 <div data-composer-body=""><div class="ProseMirror" contenteditable="true" role="textbox"
 aria-multiline="true" aria-label="Ask ChatGPT" data-composer-markdown="" data-virtualkeyboard="true"><p><br></p></div></div>
 <button type="submit" aria-label="Send">Send</button>
</form></body></html>'''

# Exercise exactly the editor and form attributes observed in the saved page.
with sync_playwright() as p:
    b=p.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--no-sandbox"])
    page=b.new_page()
    page.set_content(HTML)
    page.add_script_tag(content=SOURCE)
    prompt = 'LOCAL AGENT CHILD BOOTSTRAP\n{\n  "digest": "sha256:' + 'a'*64 + '\"\n}\nDONE'
    page.evaluate('''() => {
      window.__runs = [];
      const form = document.querySelector('form');
      const editor = document.querySelector('.ProseMirror');
      editor.addEventListener('input', () => window.__runs.push('input'));
      form.addEventListener('submit', () => {
        window.__runs.push('submit');
        editor.innerHTML = '<p></p>';
      });
    }''')
    result=page.evaluate('''async prompt => await window.LocalAgentGithubFirstComposerV1.submitOnce({
      text:prompt, routeAllowed:()=>true, beforeMutation:()=>window.__runs.push('before_mutation'),
      beforeClick:()=>window.__runs.push('before_click'), phase:step=>window.__runs.push(step)
    })''',prompt)
    events=page.evaluate('window.__runs')
    print('One-click ProseMirror browser fixture:', result, 'events:', len(events))
    assert result['submitted'] is True, (result,events)
    assert events.index('before_mutation') < events.index('before_click')
    assert events.count('submit')==1
    b.close()
