from encounters.values import digest, days, Invalid
from pathways.observations import referral_waits
from pathways.chains import build


def waiting_lineage(dataset, report, referral_id):
    options = report['options']
    view = dataset.slice(options['as_of'], options['known_at'])
    if referral_id not in view['referrals']:
        raise KeyError(referral_id)
    referral = view['referrals'][referral_id]
    source = view['visits'][referral['source']]
    target = view['visits'].get(referral['target'])
    observation = next((row for row in report['wait_observations']['rows'] if row['id'] == referral_id), None)
    excluded = next((row for row in report['wait_observations']['excluded'] if row['id'] == referral_id), None)
    records = [{'table': 'referrals', 'id': referral_id, 'digest': digest(referral)},
               {'table': 'visits', 'id': source['id'], 'digest': digest(source)},
               {'table': 'people', 'id': referral['person'], 'digest': digest(dataset.people[referral['person']])},
               {'table': 'facilities', 'id': source['facility'], 'digest': digest(dataset.facilities[source['facility']])}]
    if target:
        records.append({'table': 'visits', 'id': target['id'], 'digest': digest(target)})
    return {'referral': referral_id, 'records': records, 'observation': observation,
            'excluded': excluded, 'source_cutoffs': options,
            'formula': 'target_admission_minus_request' if target else 'as_of_minus_request',
            'unit_selection': report['waiting']['unit'],
            'excluded_as_repeated_person': referral_id in report['waiting']['excluded_repeat_referrals']}


def chain_lineage(dataset, report, root):
    chain = next((row for row in report['chains']['chains'] if row['id'] == root), None)
    if chain is None:
        raise KeyError(root)
    view = dataset.slice(report['options']['as_of'], report['options']['known_at'])
    records = [{'table': 'visits', 'id': key, 'digest': digest(view['visits'][key])} for key in chain['visits']]
    for edge in chain['edges']:
        records.append({'table': 'referrals', 'id': edge['referral'],
                        'digest': digest(view['referrals'][edge['referral']])})
    return {'root': root, 'chain_digest': chain['chain_digest'], 'records': records,
            'care_day_terms': [{'visit': key, 'days': days(view['visits'][key]['admitted'],
                               view['visits'][key]['discharged'] or view['as_of'])} for key in chain['visits']],
            'gap_day_terms': [{'referral': edge['referral'], 'days': edge['handoff_gap_days']} for edge in chain['edges']],
            'elapsed_days': chain['elapsed_days']}


def evidence_index(dataset, report):
    waits = []
    for row in report['wait_observations']['rows']:
        evidence = waiting_lineage(dataset, report, row['id'])
        waits.append({'referral': row['id'], 'lineage_digest': digest(evidence),
                      'included_in_selected_unit': not evidence['excluded_as_repeated_person']})
    chains = []
    for chain in report['chains']['chains']:
        evidence = chain_lineage(dataset, report, chain['id'])
        chains.append({'root': chain['id'], 'lineage_digest': digest(evidence)})
    body = {'input_digest': dataset.fingerprint, 'report_digest': report['report_digest'],
            'waiting_evidence': waits, 'chain_evidence': chains}
    return {**body, 'evidence_digest': digest(body)}


def verify_index(dataset, report, index):
    expected = evidence_index(dataset, report)
    if index != expected:
        raise Invalid('provenance index does not match source replay')
    return {'valid': True, 'waiting': len(index['waiting_evidence']), 'chains': len(index['chain_evidence'])}
