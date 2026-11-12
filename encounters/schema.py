import copy
from encounters.values import fields, records, text, identifier, integer, day, days, digest, unique_rows, Invalid


STAGES = ('acute', 'rehab', 'primary', 'home')
OUTCOMES = ('transfer', 'completed', 'death', 'left')
SCHEMAS = {
    'facilities': ('id', 'county', 'stage', 'name'),
    'people': ('id', 'county', 'age_band', 'rural'),
    'visits': ('id', 'person', 'facility', 'admitted', 'discharged', 'outcome', 'recorded'),
    'referrals': ('id', 'person', 'source', 'target', 'requested', 'resolved', 'status', 'destination_stage', 'recorded'),
}


def facility(row):
    fields(row, SCHEMAS['facilities'])
    identifier(row['id'])
    identifier(row['county'], 'county')
    text(row['name'], 'name')
    if row['stage'] not in STAGES:
        raise Invalid('unknown facility stage')


def person(row):
    fields(row, SCHEMAS['people'])
    identifier(row['id'])
    identifier(row['county'], 'county')
    if row['age_band'] not in ('child', 'adult', 'older'):
        raise Invalid('unknown age band')
    if type(row['rural']) is not bool:
        raise Invalid('rural must be boolean')


def visit(row):
    fields(row, SCHEMAS['visits'])
    for key in ('id', 'person', 'facility'):
        identifier(row[key], key)
    admitted = day(row['admitted'], 'admitted')
    recorded = day(row['recorded'], 'recorded')
    if recorded < admitted:
        raise Invalid('visit recorded before admission')
    if row['discharged'] is None:
        if row['outcome'] is not None:
            raise Invalid('open visit cannot have outcome')
    else:
        days(row['admitted'], row['discharged'])
        if row['outcome'] not in OUTCOMES:
            raise Invalid('closed visit requires outcome')
        if day(row['discharged']) > recorded:
            raise Invalid('discharge after recorded time')


def referral(row):
    fields(row, SCHEMAS['referrals'])
    for key in ('id', 'person', 'source'):
        identifier(row[key], key)
    day(row['requested'], 'requested')
    if day(row['recorded']) < day(row['requested']):
        raise Invalid('referral recorded before request')
    if row['destination_stage'] not in STAGES:
        raise Invalid('invalid destination stage')
    if row['status'] not in ('pending', 'accepted', 'declined', 'cancelled'):
        raise Invalid('invalid referral status')
    if row['status'] == 'pending':
        if row['resolved'] is not None or row['target'] is not None:
            raise Invalid('pending referral has resolution')
    else:
        if row['resolved'] is None:
            raise Invalid('resolved referral requires date')
        days(row['requested'], row['resolved'])
        if day(row['resolved']) > day(row['recorded']):
            raise Invalid('resolution after knowledge time')
        if row['status'] == 'accepted':
            identifier(row['target'], 'target')
        elif row['target'] is not None:
            raise Invalid('nonaccepted referral cannot link target')


VALIDATORS = {'facilities': facility, 'people': person, 'visits': visit, 'referrals': referral}


class Dataset:
    def __init__(self, document):
        fields(document, ('version', *SCHEMAS))
        if type(document['version']) is not int or document['version'] != 1:
            raise Invalid('unsupported dataset version')
        self.document = copy.deepcopy(document)
        self.duplicates = {}
        for table, validator in VALIDATORS.items():
            source = records(self.document[table], table)
            for row in source:
                validator(row)
            indexed, duplicate_count = unique_rows(source, table)
            self.duplicates[table] = duplicate_count
            setattr(self, table, indexed)
            self.document[table] = [indexed[key] for key in sorted(indexed)]
        self._references()
        self.fingerprint = digest(self.document)

    def _references(self):
        for row in self.visits.values():
            if row['person'] not in self.people or row['facility'] not in self.facilities:
                raise Invalid('visit references missing person/facility')
        targets = {}
        sources = {}
        for row in self.referrals.values():
            if row['person'] not in self.people or row['source'] not in self.visits:
                raise Invalid('referral references missing person/source')
            source = self.visits[row['source']]
            if source['person'] != row['person']:
                raise Invalid('referral source person mismatch')
            if day(row['requested']) < day(source['admitted']):
                raise Invalid('referral before source admission')
            if row['target'] is None:
                continue
            if row['target'] not in self.visits:
                raise Invalid('referral target missing')
            target = self.visits[row['target']]
            if target['person'] != row['person'] or target['id'] == source['id']:
                raise Invalid('referral target person mismatch or self link')
            if self.facilities[target['facility']]['stage'] != row['destination_stage']:
                raise Invalid('target stage mismatch')
            if day(target['admitted']) < day(row['requested']):
                raise Invalid('target admitted before referral request')
            if day(row['resolved']) > day(target['admitted']):
                raise Invalid('referral accepted after target admission')
            if day(target['recorded']) > day(row['recorded']):
                raise Invalid('referral references future knowledge')
            if source['discharged'] is None or source['outcome'] != 'transfer':
                raise Invalid('linked source must be discharged as transfer')
            if day(target['admitted']) < day(source['discharged']):
                raise Invalid('linked visits overlap')
            if row['source'] in sources or row['target'] in targets:
                raise Invalid('ambiguous accepted continuation links')
            sources[row['source']] = row['target']
            targets[row['target']] = row['source']
        for start in sources:
            visited = set()
            node = start
            while node in sources:
                if node in visited:
                    raise Invalid('referral cycle')
                visited.add(node)
                node = sources[node]

    def export(self):
        return copy.deepcopy(self.document)

    def stage(self, visit_id):
        return self.facilities[self.visits[visit_id]['facility']]['stage']

    def slice(self, as_of, known_at):
        business = day(as_of, 'as_of')
        knowledge = day(known_at, 'known_at')
        if knowledge < business:
            raise Invalid('known_at cannot precede as_of')
        visits = {}
        hidden_closures = []
        for key, row in self.visits.items():
            if day(row['recorded']) > knowledge or day(row['admitted']) > business:
                continue
            item = copy.deepcopy(row)
            if item['discharged'] and day(item['discharged']) > business:
                item['discharged'] = None
                item['outcome'] = None
                hidden_closures.append(key)
            visits[key] = item
        referrals = {}
        for key, row in self.referrals.items():
            if day(row['recorded']) > knowledge or day(row['requested']) > business or row['source'] not in visits:
                continue
            item = copy.deepcopy(row)
            if item['resolved'] and day(item['resolved']) > business:
                item.update(status='pending', resolved=None, target=None)
            if item['target'] is not None and item['target'] not in visits:
                item.update(status='pending', resolved=None, target=None)
            referrals[key] = item
        return {'visits': visits, 'referrals': referrals, 'as_of': as_of,
                'known_at': known_at, 'hidden_future_closures': sorted(hidden_closures),
                'input_digest': self.fingerprint}
