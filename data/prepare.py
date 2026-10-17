"""Normalize the public snapshot for the four-resource baseline.

No country or administrative code is joined across providers. Unresolved source
references are counted explicitly. Original immutable downloads remain available.
"""
import collections
import csv
import io
import json
from pathlib import Path
import sys
import zipfile

from fetch import digest, verify

CITY_CODES = {'PPL', 'PPLA', 'PPLA2', 'PPLA3', 'PPLA4', 'PPLA5', 'PPLC'}


def tsv(path):
    with path.open(encoding='utf-8', newline='') as stream:
        yield from csv.reader((line for line in stream if line.strip() and not line.startswith('#')), delimiter='\t')


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        yield from csv.DictReader(stream)


def identifier(source, kind, value):
    return f'{source}:{kind}:{value}'


def prepare(source, output):
    manifest = verify(source)
    if manifest['profile'] != 'cities500':
        raise ValueError('This initial normalizer supports the cities500 profile only')
    output.mkdir(parents=True, exist_ok=False)
    counts = collections.Counter()
    exclusions = collections.Counter()
    unresolved = collections.Counter()
    city_links = collections.Counter()
    countries = {}
    divisions = {}
    seen = set()

    def emit(stream, kind, record):
        if record['id'] in seen:
            raise ValueError(f"Duplicate normalized id: {record['id']}")
        seen.add(record['id'])
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
        counts[kind] += 1

    with (output/'countries.jsonl').open('x', encoding='utf-8') as stream:
        for row in tsv(source/'countryInfo.txt'):
            rid = identifier('geonames', 'country', row[16])
            countries['geonames', row[0]] = rid
            emit(stream, 'countries', dict(id=rid, source='geonames', name=row[4], country_code=row[0], continent=row[8]))
        for row in csv_rows(source/'countries.csv'):
            rid = identifier('ourairports', 'country', row['id'])
            countries['ourairports', row['code']] = rid
            emit(stream, 'countries', dict(id=rid, source='ourairports', name=row['name'], country_code=row['code'], continent=row['continent']))

    with (output/'divisions.jsonl').open('x', encoding='utf-8') as stream:
        for level, filename in [(1, 'admin1CodesASCII.txt'), (2, 'admin2Codes.txt')]:
            for code, name, ascii_name, gid in tsv(source/filename):
                country_code = code.split('.')[0]
                country_id = countries.get(('geonames', country_code))
                if country_id is None:
                    exclusions['division_country_missing'] += 1
                    continue
                parent_id = divisions.get(('geonames', code.rsplit('.', 1)[0])) if level == 2 else None
                if level == 2 and parent_id is None:
                    unresolved['division_parent'] += 1
                rid = identifier('geonames', 'division', gid)
                divisions['geonames', code] = rid
                emit(stream, 'divisions', dict(id=rid, source='geonames', name=name, country_id=country_id, code=code, level=level, parent_id=parent_id))
        for row in csv_rows(source/'regions.csv'):
            if row['code'].endswith('-U-A'):
                exclusions['airport_region_unassigned_placeholder'] += 1
                continue
            country_id = countries['ourairports', row['iso_country']]
            rid = identifier('ourairports', 'division', row['id'])
            divisions['ourairports', row['code']] = rid
            emit(stream, 'divisions', dict(id=rid, source='ourairports', name=row['name'], country_id=country_id, code=row['code'], level=1, parent_id=None))

    with (output/'cities.jsonl').open('x', encoding='utf-8') as stream, (output/'city-link-provenance.jsonl').open('x', encoding='utf-8') as provenance, zipfile.ZipFile(source/'cities500.zip') as archive:
        with archive.open('cities500.txt') as raw:
            for row in csv.reader(io.TextIOWrapper(raw, encoding='utf-8'), delimiter='\t'):
                if row[6] != 'P' or row[7] not in CITY_CODES:
                    exclusions['city_feature_' + row[7]] += 1
                    continue
                country_id = countries.get(('geonames', row[8]))
                if country_id is None:
                    exclusions['city_country_missing'] += 1
                    continue
                admin1 = '.'.join([row[8], row[10]])
                admin2 = admin1 + '.' + row[11]
                exact_admin2 = divisions.get(('geonames', admin2)) if row[11] else None
                known_admin1 = divisions.get(('geonames', admin1))
                division_id = exact_admin2 or known_admin1
                city_links['exact_admin2' if exact_admin2 else 'admin1' if known_admin1 else 'none'] += 1
                missing_admin2 = bool(row[11]) and exact_admin2 is None
                if missing_admin2:
                    unresolved['city_nonempty_admin2_missing'] += 1
                    if known_admin1:
                        unresolved['city_admin2_missing_fallback_to_admin1'] += 1
                if division_id is None:
                    unresolved['city_division'] += 1
                if missing_admin2 or division_id is None:
                    provenance.write(json.dumps(dict(id=identifier('geonames', 'city', row[0]), country_code=row[8], admin1_code=row[10], admin2_code=row[11], admin3_code=row[12], admin4_code=row[13], selected_division_id=division_id), ensure_ascii=False) + '\n')
                emit(stream, 'cities', dict(id=identifier('geonames', 'city', row[0]), source='geonames', name=row[1], country_id=country_id, division_id=division_id, latitude=float(row[4]), longitude=float(row[5]), population=int(row[14]), feature_code=row[7]))

    with (output/'airports.jsonl').open('x', encoding='utf-8') as stream:
        for row in csv_rows(source/'airports.csv'):
            if row['scheduled_service'] not in {'yes', 'no'}:
                raise ValueError(f"Unexpected scheduled_service for OurAirports {row['id']}: {row['scheduled_service']!r}")
            region_id = divisions.get(('ourairports', row['iso_region']))
            if region_id is None:
                unresolved['airport_region'] += 1
            emit(stream, 'airports', dict(id=identifier('ourairports', 'airport', row['id']), source='ourairports', name=row['name'], country_id=countries['ourairports', row['iso_country']], region_id=region_id, city_id=None, latitude=float(row['latitude_deg']), longitude=float(row['longitude_deg']), airport_type=row['type'], scheduled_service=row['scheduled_service']=='yes', municipality=row['municipality'] or None, iata_code=row['iata_code'] or None))
    output_files = {p.name: {'sha256': digest(p), 'bytes': p.stat().st_size} for p in sorted(output.glob('*.jsonl'))}
    report = {'transform_version': 2, 'profile': manifest['profile'], 'city_feature_codes': sorted(CITY_CODES), 'counts': dict(counts), 'exclusions': dict(exclusions), 'unresolved': dict(unresolved), 'city_division_selection': dict(city_links), 'division_id_semantics': 'Deepest available GeoNames ADM1/ADM2; raw unresolved paths in city-link-provenance.jsonl and all original rows in source snapshot', 'source_manifest': manifest, 'output_files': output_files}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key: report[key] for key in ('counts', 'exclusions', 'unresolved')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
