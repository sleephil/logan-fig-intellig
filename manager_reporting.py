"""Manager-first 13F attribution, never an estimate of total AUM or client flows.

Sequence numbers are local to an accession. Use stable manager IDs across dates;
keep jointly attributed, unassigned and options values separately reconcilable.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date
import hashlib
import re

METHOD_VERSION = 'manager-attribution-1'
LIMITATIONS = [
    'Assets shown are holdings reported for this legal filer and linked to each other included manager, not the manager\'s total AUM or a complete platform allocation census.',
    'The SEC other-included-manager list can include affiliates; external ownership is not independently established.',
    'Single-linked assets are assigned to exactly one listed manager. Jointly linked assets are separate and must not be summed across managers.',
    'Unassigned and unresolved holdings are shown in reconciliation, not assumed to be managed in house. Options reference values are excluded from asset totals.',
    'Quarterly changes include market performance, trades, corporate actions and reporting changes. Paired increases and decreases are research leads, not measured cash flows or confirmed transfers.',
    'A manager not listed for a period is marked not reported, not zero assets. Missing quarters and incomplete amendment chains are not bridged for change calculations.',
]


def normalized_name(value):
    return ' '.join(str(value or '').upper().split())


def identifiers(manager):
    ids = []
    cik = str(manager.get('cik') or '').strip()
    if re.fullmatch(r'\d{1,10}', cik) and int(cik):
        ids.append('cik:' + str(int(cik)).zfill(10))
    file_no = str(manager.get('form13f_file_number') or '').strip()
    match = re.fullmatch(r'0?28\s*-\s*(\d+)', file_no)
    if match and int(match[1]):
        ids.append('13f:028-' + str(int(match[1])).zfill(5))
    return ids


class IdentityIndex:
    def __init__(self, snapshots):
        self.parents = {}; self.meta = {}; self.names = defaultdict(set)
        def root(key):
            self.parents.setdefault(key, key)
            while self.parents[key] != key:
                self.parents[key] = self.parents[self.parents[key]]
                key = self.parents[key]
            return key
        for snap in snapshots:
            for m in snap.managers:
                ids = identifiers(m)
                if ids:
                    chosen = min(root(i) for i in ids)
                    for i in ids: self.parents[root(i)] = chosen
        self.root = root
        groups = defaultdict(set)
        for key in self.parents: groups[root(key)].add(key)
        # Conflicting CIKs are not merged into a believable single manager.
        self.conflicts = {r for r, ids in groups.items() if sum(i.startswith('cik:') for i in ids) > 1}
        self.canonical = {r: min(ids, key=lambda i: (not i.startswith('cik:'), i)) for r, ids in groups.items()}
        for snap in snapshots:
            for m in snap.managers:
                ids = identifiers(m); name = normalized_name(m.get('name'))
                if ids and name and root(ids[0]) not in self.conflicts:
                    self.names[name].add(self.canonical[root(ids[0])])
        for snap in sorted(snapshots, key=lambda s: (s.period, s.filing_date, s.accession)):
            for m in snap.managers:
                key = self.key(m, snap)
                if key is None: continue
                previous = self.meta.get(key, {})
                aliases = set(previous.get('names', [])) | {str(m.get('name') or '').strip()}
                self.meta[key] = {'id': key, 'name': str(m.get('name') or '').strip(),
                    'names': sorted(aliases), 'cik': m.get('cik') or previous.get('cik', ''),
                    'form13f_file_number': m.get('form13f_file_number') or previous.get('form13f_file_number', ''),
                    'identity_basis': 'Stable SEC identifier' if not key.startswith('name:') else 'Exact reported name; no stable identifier'}

    def key(self, manager, snap):
        ids = identifiers(manager); name = normalized_name(manager.get('name'))
        if not name: return None
        if ids:
            r = self.root(ids[0])
            return None if r in self.conflicts else self.canonical[r]
        candidates = self.names.get(name, set())
        if len(candidates) > 1: return None
        if candidates: return next(iter(candidates))
        return 'name:' + hashlib.sha256(name.encode()).hexdigest()[:20]


def sequence(value):
    value = str(value or '').strip()
    return str(int(value)) if re.fullmatch(r'\d+', value) and int(value) > 0 else None


def references(value):
    value = str(value or '').strip()
    if value.upper() in ('', 'N/A', 'NA', 'NONE'): return [], False
    if not re.fullmatch(r'\d+(?:[\s,;]+\d+)*', value): return [], True
    parsed = [sequence(v) for v in re.split(r'[\s,;]+', value)]
    return sorted({v for v in parsed if v is not None}), any(v is None for v in parsed)


def adjacent(prior, current):
    a, b = date.fromisoformat(prior), date.fromisoformat(current)
    return (b.year * 4 + (b.month - 1)//3) - (a.year * 4 + (a.month - 1)//3) == 1


def pct(change, previous):
    return round(change / previous * 100, 4) if change is not None and previous else None


def manager_report(snapshots, *, identity_index=None):
    snapshots = list(snapshots)
    if len({s.cik for s in snapshots}) > 1: raise ValueError('MIXED_FILER_MANAGER_REPORT')
    seen = {}; unique = []
    for s in snapshots:
        s.validate()
        if s.accession in seen:
            if s.checksum != seen[s.accession]: raise ValueError('CONFLICTING_ACCESSION_REVISIONS')
            continue
        seen[s.accession] = s.checksum; unique.append(s)
    identities = identity_index or IdentityIndex(unique)
    groups = defaultdict(list)
    for s in unique: groups[s.period].append(s)
    periods = []; issues = []; history = defaultdict(dict); summaries = []
    for period, versions in sorted(groups.items()):
        effective = []; roster = set(); used = []; period_issues = []; has_base = False
        for s in sorted(versions, key=lambda s: (s.filing_date, s.amendment_no, s.accession)):
            if s.form == '13F-HR' or s.amendment_type == 'RESTATEMENT':
                effective = []; roster = set(); used = []; period_issues = []; has_base = True
            elif s.amendment_type != 'NEW HOLDINGS' or not has_base:
                period_issues.append({'period': period, 'accession': s.accession, 'code': 'AMENDMENT_BASE_MISSING'})
                continue
            mapping = {}; invalid = set()
            for m in s.managers:
                key = identities.key(m, s); seq = sequence(m.get('sequence'))
                if key: roster.add(key)
                if not seq or key is None or (seq in mapping and mapping[seq] != key):
                    invalid.add(seq)
                    period_issues.append({'period': period, 'accession': s.accession, 'code': 'AMBIGUOUS_MANAGER_IDENTITY_OR_SEQUENCE'})
                elif seq not in invalid: mapping[seq] = key
            for seq in invalid: mapping.pop(seq, None)
            for row in s.rows:
                refs, malformed = references(row.get('other_manager'))
                unknown = malformed or any(ref not in mapping for ref in refs)
                linked = sorted({mapping[ref] for ref in refs if ref in mapping})
                # A partially resolved multi-manager link is not a single-manager assignment.
                effective.append((row, linked, unknown))
            used.append(s)
        issues.extend(period_issues)
        if not has_base: continue
        periods.append(period)
        totals = dict(single_linked_usd=0, jointly_linked_usd=0, unassigned_usd=0, unresolved_usd=0, options_reference_usd=0)
        for key in roster:
            history[key][period] = {'period': period, 'reported_assets_usd': 0, 'single_linked_usd': 0,
                'jointly_linked_usd': 0, 'options_reference_usd': 0, 'status': 'listed', 'comparisons_allowed': not period_issues}
        for row, linked, unknown in effective:
            value = row['value_usd']
            if row['put_call']:
                totals['options_reference_usd'] += value
                if not unknown:
                    for key in linked: history[key][period]['options_reference_usd'] += value
                continue
            if unknown: totals['unresolved_usd'] += value; continue
            if not linked: totals['unassigned_usd'] += value; continue
            bucket = 'single_linked_usd' if len(linked) == 1 else 'jointly_linked_usd'
            totals[bucket] += value
            for key in linked:
                history[key][period][bucket] += value
                history[key][period]['reported_assets_usd'] += value
        if totals['unresolved_usd']:
            issues.append({'period': period, 'code': 'UNRESOLVED_ROW_MANAGER_REFERENCES'})
            for key in roster: history[key][period]['comparisons_allowed'] = False
        expected = sum(s.expected_total for s in used)
        if sum(totals.values()) != expected: raise ValueError('MANAGER_RECONCILIATION_MISMATCH')
        summaries.append({'period': period, **totals, 'reported_total_usd': expected,
            'attributed_unique_usd': totals['single_linked_usd'] + totals['jointly_linked_usd'],
            'listed_managers': len(roster), 'reconciled': True,
            'comparison_quality': 'review' if period_issues or totals['unresolved_usd'] else 'complete',
            'sources': [{'accession': s.accession, 'filing_date': s.filing_date, 'snapshot_sha256': s.checksum, 'sources': s.sources} for s in used]})
    managers = []
    for key, values in history.items():
        entries = []
        for p in periods:
            entry = dict(values.get(p, {'period': p, 'reported_assets_usd': None, 'single_linked_usd': None,
                'jointly_linked_usd': None, 'options_reference_usd': None, 'status': 'not_reported', 'comparisons_allowed': False}))
            prior = entries[-1] if entries else None
            comparable = bool(prior and adjacent(prior['period'], p) and entry['comparisons_allowed'] and prior['comparisons_allowed'])
            change = entry['reported_assets_usd'] - prior['reported_assets_usd'] if comparable else None
            entry.update(previous_value_usd=prior['reported_assets_usd'] if comparable else None,
                change_usd=change, change_pct=pct(change, prior['reported_assets_usd']) if comparable else None,
                comparable=comparable)
            year = str(int(p[:4])-1)+p[4:]; year_entry = values.get(year)
            year_ok = entry['comparisons_allowed'] and year_entry and year_entry['comparisons_allowed']
            yoy = entry['reported_assets_usd']-year_entry['reported_assets_usd'] if year_ok else None
            entry.update(year_change_usd=yoy, year_change_pct=pct(yoy, year_entry['reported_assets_usd']) if year_ok else None)
            entries.append(entry)
        managers.append({**identities.meta[key], 'history': entries})
    managers.sort(key=lambda m: (-(m['history'][-1]['reported_assets_usd'] or 0), m['name'].upper()))
    pairs = []
    for i, period in enumerate(periods):
        if not i: continue
        increasing = []; decreasing = []
        for m in managers:
            now, before = m['history'][i], m['history'][i-1]
            if not now['comparable'] or now['jointly_linked_usd'] or before['jointly_linked_usd']: continue
            change = now['change_usd']
            if change > 0: increasing.append((m, change))
            elif change < 0: decreasing.append((m, -change))
        candidates = []
        for down, loss in decreasing:
            for up, gain in increasing:
                ratio = min(loss, gain)/max(loss, gain)
                if ratio < .5: continue
                candidates.append({'period': period, 'previous_period': periods[i-1], 'declining_id': down['id'],
                    'declining': down['name'], 'increasing_id': up['id'], 'increasing': up['name'],
                    'decrease_usd': loss, 'increase_usd': gain, 'difference_usd': abs(gain-loss),
                    'magnitude_similarity': round(ratio, 4),
                    'interpretation': 'Similar opposite reported-value changes in the same quarter; not verified flows or transfers.'})
        # Every candidate is independently ranked, not an additive allocation of flows.
        candidates.sort(key=lambda r: (-r['magnitude_similarity'], -min(r['decrease_usd'], r['increase_usd']), r['declining_id'], r['increasing_id']))
        pairs.extend(candidates[:20])
    return {'schema_version': 1, 'method_version': METHOD_VERSION, 'periods': periods, 'managers': managers,
        'period_summaries': summaries, 'possible_pairs': pairs, 'issues': issues, 'limitations': LIMITATIONS,
        'pair_method': 'Up to 20 pairs per quarter; opposite reported-value changes with smaller/larger magnitude at least 50%. Requires adjacent complete quarters and no jointly linked assets. Similarity is not a probability of transfer.'}
