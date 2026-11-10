"""Manually test real Gemini classification using one synthetic paper.

Never run the production feed loop, load a private ranking profile, write feed
outputs, deploy, or send email. API calls are bounded by the existing key/model
rotation and retry policy. A successful test proves one real classification,
not that the production backlog has been drained.
"""

import os

# Guard against accidentally supplying private profile data to this smoke test.
for _name in list(os.environ):
    if _name.startswith('RSS_'):
        os.environ.pop(_name)

import Filter_RSS as rss


def main():
    fixture = {
        'title': 'Synthetic test fixture: electronic structure in a crystalline solid',
        'summary': ('This is synthetic input for an API compatibility test, not an actual paper. '
                    'It describes measuring the electronic band structure of a crystalline solid.'),
        'link': 'https://example.invalid/synthetic-gemini-smoke-test',
    }
    passed, removed, pending, _ = rss.classify_entries_with_gemini('Nature', [fixture])
    classified = len(passed) + len(removed)
    rss.check_classification_health(classified, len(pending))
    if classified != 1 or pending:
        raise RuntimeError('Synthetic smoke test did not return exactly one classification.')
    print('PASS: one synthetic paper classified through the live Gemini API.')
    print('Production feeds, pending queue, Pages, and email were not touched.')


if __name__ == '__main__':
    main()
