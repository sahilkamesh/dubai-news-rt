from main import get_recent_megathread_links, fetch_reddit_comments_from_json

def test_get_recent_megathread_links():
    links = get_recent_megathread_links()
    print('Megathread links:', links)
    assert isinstance(links, list)
    assert all(link.endswith('.json') for link in links)
    assert len(links) > 0, 'No megathread links found.'
    print('test_get_recent_megathread_links passed')

def test_fetch_reddit_comments_from_json():
    links = get_recent_megathread_links()
    if not links:
        print('No megathread links to test comments.')
        return
    comments = fetch_reddit_comments_from_json(links[0], max_comments=5)
    print('Comments:', comments)
    assert isinstance(comments, list)
    assert len(comments) > 0, 'No comments found for megathread.'
    assert 'text' in comments[0]
    print('test_fetch_reddit_comments_from_json passed')

if __name__ == '__main__':
    test_get_recent_megathread_links()
    test_fetch_reddit_comments_from_json()
