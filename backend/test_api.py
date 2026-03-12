import requests

def test_news():
    resp = requests.get('http://localhost:8000/news')
    assert resp.status_code == 200
    data = resp.json()
    print(data)
    assert isinstance(data, list)
    # assert any('X (@' in item['source'] for item in data), 'No real X (Twitter) data found'
    print('test_news passed')

def test_areas():
    resp = requests.get('http://localhost:8000/areas')
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any('area' in item for item in data)
    print('test_areas passed')

if __name__ == '__main__':
    test_news()
    test_areas()
