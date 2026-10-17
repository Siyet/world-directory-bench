"""Small real-TCP correctness workload, not a performance benchmark."""
import argparse
import asyncio
from collections import Counter
import heapq
import json
import os
from pathlib import Path

import httpx


def records(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            yield json.loads(line)


def expected_page(path, criteria, countries, limit=20):
    def include(row):
        for key, value in criteria.items():
            if key == 'country_code':
                code = row.get('country_code') if 'country_id' not in row else countries[row['country_id']]['country_code']
                if code != value:
                    return False
            elif key == 'min_population':
                if row['population'] < value:
                    return False
            elif row.get(key) != value:
                return False
        return True
    selected = heapq.nsmallest(limit + 1, (r for r in records(path) if include(r)), key=lambda r: r['id'])
    return {'items': selected[:limit], 'next_after_id': selected[limit - 1]['id'] if len(selected) > limit else None}


async def main(directory, url):
    countries = {r['id']: r for r in records(directory / 'countries.jsonl')}
    cases = []
    for resource in ['countries', 'divisions', 'cities', 'airports']:
        criteria_list = [{}] + [{'country_code': c} for c in ['BR', 'US', 'ZA', 'JP', 'AU', 'DE']]
        if resource == 'cities':
            criteria_list += [{'country_code': 'BR', 'min_population': 100000}]
        if resource == 'airports':
            criteria_list += [{'airport_type': 'closed'}, {'scheduled_service': True}]
        if resource == 'divisions':
            criteria_list += [{'source': 'geonames', 'level': 2}]
        for criteria in criteria_list:
            expected = expected_page(directory / (resource + '.jsonl'), criteria, countries)
            assert expected['items'], (resource, criteria)
            params = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in criteria.items()}
            cases.append(('/' + resource, {**params, 'limit': 20}, expected))
            first = expected['items'][0]
            cases.append(('/' + resource + '/' + first['id'], {}, first))
    counts = Counter()
    async with httpx.AsyncClient(base_url=url, timeout=20, limits=httpx.Limits(max_connections=8)) as client:
        for attempt in range(30):
            try:
                ready = await client.get('/openapi.json')
                ready.raise_for_status()
                break
            except (httpx.ConnectError, httpx.HTTPStatusError):
                if attempt == 29:
                    raise
                await asyncio.sleep(0.2)
        # Sequential first-touch requests record real misses, followed by concurrent repeats.
        for path, params, expected in cases:
            response = await client.get(path, params=params)
            assert response.status_code == 200, response.text
            assert response.json() == expected, (path, params)
            counts[response.headers['X-Cache']] += 1

        async def worker(index):
            for step in range(40):
                path, params, expected = cases[(index * 17 + step) % len(cases)]
                response = await client.get(path, params=params)
                assert response.status_code == 200, response.text
                assert response.json() == expected
                counts[response.headers['X-Cache']] += 1
        await asyncio.gather(*(worker(i) for i in range(8)))

        country = {'id': 'local:country:tcp-smoke', 'source': 'local', 'name': 'TCP smoke', 'country_code': 'XX', 'continent': 'AF'}
        assert (await client.post('/countries', json=country)).status_code == 201
        async def write_worker(index):
            body = {'id': f'local:city:tcp-smoke-{index}', 'source': 'local', 'name': 'Before', 'country_id': country['id'], 'division_id': None, 'latitude': 1.0, 'longitude': 2.0, 'population': 50, 'feature_code': 'PPL'}
            assert (await client.post('/cities', json=body)).status_code == 201
            path = '/cities/' + body['id']
            assert (await client.get(path)).json() == body
            body['name'] = 'After committed TCP write'
            assert (await client.put(path, json=body)).json() == body
            assert (await client.get(path)).json() == body
            assert (await client.delete(path)).status_code == 204
            assert (await client.get(path)).status_code == 404
        await asyncio.gather(*(write_worker(i) for i in range(8)))
        assert (await client.delete('/countries/' + country['id'])).status_code == 204
        assert (await client.get('/cities', params={'country_code': 'XX', 'source': 'local'})).json()['items'] == []
    return {'response_path': os.environ.get('RESPONSE_PATH', 'explicit'), 'transport': 'TCP HTTP/1.1 to Uvicorn, asyncio/h11, 1 worker', 'read_cases': len(cases), 'checked_read_responses': sum(counts.values()), 'cache_results': dict(counts), 'concurrent_clients': 8, 'concurrent_city_crud_cycles': 8, 'full_json_equality': True, 'performance_result': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('normalized_directory', type=Path)
    parser.add_argument('--url', default='http://api:8000')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main(args.normalized_directory, args.url)), indent=2))
