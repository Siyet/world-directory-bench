"""Page validation and the explicit presentation-to-Response boundary."""
import json
from collections import Counter
import sys

import httpx
import pytest
import msgspec
from directory.api import app
from directory.responses import RESPONSE_PATH
from directory.schema_ops import MSGSPEC, schemas as s, validate
from test_contract import client, seed, CITY


def test_page_mapping_validates_structure_without_revalidating_existing_item():
    if not MSGSPEC:
        pytest.skip('msgspec model boundary')
    item = validate(s.City, CITY)
    counts = Counter()
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == '__post_init__':
            counts['post_init'] += 1
    sys.setprofile(profile)
    try:
        page = validate(s.Page[s.City], {'items': [item], 'next_after_id': item.id})
    finally:
        sys.setprofile(None)
    assert page.items == [item] and page.items[0] is item
    assert counts['post_init'] == 0
    for body in [{'items': None, 'next_after_id': None}, {'items': [None], 'next_after_id': None}, {'items': [item], 'next_after_id': 1}, {'next_after_id': None}, {'items': [item], 'next_after_id': None, 'extra': 1}]:
        with pytest.raises(msgspec.ValidationError):
            validate(s.Page[s.City], body)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'items': None}, {'items': [None]}, {'next_after_id': 42},
    {'extra': 'PRIVATE-LEAK'}, {'next_after_id': '\ud800'},
])
async def test_cached_page_structure_or_unicode_is_rejected(client, change):
    if RESPONSE_PATH != 'msgspec-json-response':
        pytest.skip('explicit msgspec Response boundary')
    await seed(client)
    assert (await client.get('/cities')).status_code == 200
    cache = app.state.service.cache
    entries = [(key, value) async for key, value in cache.get_match('wd:v1:*:cities:*')]
    assert len(entries) == 1
    key, page = entries[0]
    await cache.set(key, dict(page, **change), expire=60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as safe:
        response = await safe.get('/cities')
    assert response.status_code == 500
    assert response.content == b'Internal Server Error'
