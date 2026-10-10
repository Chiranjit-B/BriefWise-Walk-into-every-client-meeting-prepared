#!/usr/bin/env python3
"""Reproducible synthetic wealth-advisor fixtures. Python 3.9+, standard library only."""
import argparse
import calendar
import csv
import hashlib
import io
import json
import random
import shutil
import statistics
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import zipfile

SEED = 42
HEADERS = ['First name', 'Last name', 'Email', 'Client ID', 'Risk tolerance', 'Financial goals', 'Advisor context']
CATEGORIES = ['groceries', 'utilities', 'transport', 'dining', 'shopping', 'healthcare']
SCENARIOS = ['expected_furniture_purchase','increasing_spending','negative_cash_flow','suspicious_transfer_burst','suspicious_large_transfer']
RULES = dict(zip(SCENARIOS, [['R2'],['R2'],['R3'],['R4'],['R1','R2','R5']]))


def money(cents):
    return Decimal(cents) / 100

def cents(value):
    d = Decimal(str(value)) * 100
    assert d == d.to_integral_value(), 'More than two decimal places'
    return int(d)

def json_text(value):
    # Emit Decimal as JSON number without introducing binary floating-point arithmetic.
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + ':' + json_text(v) for k,v in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join(json_text(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)

def write_json(path, value):
    path.write_text(json_text(value) + '\n', encoding='utf-8')

def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'), parse_float=Decimal)

def evaluate(client, profile):
    """Independent detector: no scenario IDs or generation totals are consulted."""
    tx=client['transactions']
    totals={m:sum(cents(t['amount']) for t in tx if datetime.fromisoformat(t['timestamp']).month==m and t['direction']=='debit') for m in range(1,7)}
    june=[t for t in tx if datetime.fromisoformat(t['timestamp']).month==6]
    income=sum(cents(t['amount']) for t in june if t['direction']=='credit' and t['transaction_type']=='salary')
    findings=[]
    context=profile['advisor_context'] if profile else None
    def finding(rule,typ,evidence,threshold,question,ids=None):
        f={'anomaly_type':typ,'rule_id':rule,'numeric_evidence':evidence,'threshold':threshold,'relevant_crm_context':context,'suggested_verification_question':question}
        if ids is None: f['affected_month']='2026-06'
        else: f['transaction_ids']=ids
        findings.append(f)
    r1_threshold=Decimal(income)*Decimal('0.5')
    if income>0:
        for t in june:
            if t['direction']=='debit' and Decimal(cents(t['amount']))>r1_threshold:
                finding('R1','income_relative_large_debit',{'amount':t['amount'],'salary_basis':money(income),'threshold_amount':r1_threshold/100}, {'amount_strictly_greater_than_50_percent_of_salary':r1_threshold/100}, 'Can you confirm the purpose and authorization of this payment?', [t['transaction_id']])
    baseline=Decimal(sum(totals[m] for m in (3,4,5)))/3
    if Decimal(totals[6])>baseline*Decimal('1.5'):
        finding('R2','spending_spike',{'baseline_months':['2026-03','2026-04','2026-05'],'baseline_average':baseline/100,'june_total':money(totals[6]),'percentage_increase':((Decimal(totals[6])/baseline-1)*100).quantize(Decimal('.01'))},{'multiplier_strictly_greater_than':Decimal('1.5')},'What explains the increase in June spending, and how does it affect your savings plan?')
    if totals[6]>income:
        finding('R3','negative_cash_flow',{'income':money(income),'outgoing_spending':money(totals[6]),'deficit':money(totals[6]-income)},{'outgoing_spending_strictly_greater_than_income':money(income)},'Is this spending temporary, and how should we adjust the budget or savings contributions?')
    transfers=[t for t in tx if t['direction']=='debit' and t['transaction_type']=='transfer']
    historical=[cents(t['amount']) for t in transfers if datetime.fromisoformat(t['timestamp']).month<6]
    median=statistics.median([Decimal(v) for v in historical]) if len(historical)>=5 else None
    seen=set()
    for t in transfers:
        new=t['recipient_id'] not in seen
        dt=datetime.fromisoformat(t['timestamp'])
        if dt.month==6 and new:
            window=[u for u in transfers if u['recipient_id']==t['recipient_id'] and dt<=datetime.fromisoformat(u['timestamp'])<=dt+timedelta(minutes=30)]
            if len(window)>=5:
                duration=(datetime.fromisoformat(window[-1]['timestamp'])-dt).total_seconds()/60
                finding('R4','suspicious transfer pattern',{'recipient_id':t['recipient_id'],'timestamps':[u['timestamp'] for u in window],'window_duration_minutes':Decimal(str(duration)),'count':len(window),'total':money(sum(cents(u['amount']) for u in window))},{'minimum_count':5,'inclusive_window_minutes':30,'new_within_observed_history':True},'Do you recognize this recipient and did you authorize all of these payments?', [u['transaction_id'] for u in window])
            if median is not None and cents(t['amount'])>500000 and Decimal(cents(t['amount']))>median*5:
                finding('R5','potential fraud concern',{'recipient_id':t['recipient_id'],'amount':t['amount'],'historical_transfer_count':len(historical),'historical_median':median/100,'amount_to_median_ratio':(Decimal(cents(t['amount']))/median).quantize(Decimal('.01'))},{'amount_strictly_greater_than':5000,'median_multiplier_strictly_greater_than':5,'new_within_observed_history':True},'Do you recognize the recipient and authorize this transfer? Please review it with your advisor.',[t['transaction_id']])
        seen.add(t['recipient_id'])
    return {'client_id':client['client_id'],'expected_triggered_rule_ids':sorted(set(f['rule_id'] for f in findings)),'expected_findings':findings,'expected_warnings':[] if profile else ['CRM profile unavailable: goals, risk tolerance, and advisor context are unavailable.'],'human_review_required':any(f['rule_id'] in ('R4','R5') for f in findings)},income,totals[6]

def derive_monthly_r1(data):
    result={}
    for month_number in range(1,7):
        month=f'2026-{month_number:02d}';triggered={};not_evaluated=[]
        for client in data['clients']:
            selected=[t for t in client['transactions'] if datetime.fromisoformat(t['timestamp']).strftime('%Y-%m')==month]
            salary=sum((money(cents(t['amount'])) for t in selected if t['direction']=='credit' and t['transaction_type']=='salary'),Decimal('0'))
            if salary<=0:not_evaluated.append(client['client_id']);continue
            threshold=salary*Decimal('0.5')
            hits=[{'transaction_id':t['transaction_id'],'amount':str(t['amount'])} for t in selected if t['direction']=='debit' and Decimal(str(t['amount']))>threshold]
            if hits:triggered[client['client_id']]={'salary_basis':str(salary),'threshold_amount':str(threshold),'transactions':hits}
        result[month]={'triggered':triggered,'not_evaluated':sorted(not_evaluated)}
    return result

def read_source(path):
    with path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f);assert reader.fieldnames==HEADERS
        rows=list(reader)
    assert len(rows)==24 and {r['Client ID'] for r in rows}=={f'C{i:03}' for i in range(1,26)}-{'C005'}
    assert len({r['Email'] for r in rows})==24
    return rows

def generate(source):
    # Assignment RNG is independent of every later draw and follows source row order.
    chosen=random.Random(42).sample([r['Client ID'] for r in source],5)
    mapping=dict(zip(chosen,SCENARIOS))
    rng=random.Random(42)
    clients=[];updated=[]
    source_map={r['Client ID']:r for r in source}
    for i in range(1,26):
        cid=f'C{i:03}';scenario=mapping.get(cid,'ordinary')
        salary=rng.randrange(45,101)*100
        base=salary*rng.randint(46,59)//100
        if scenario=='expected_furniture_purchase':salary,base=12000,3500
        if scenario=='increasing_spending':salary,base=8500,2200
        if scenario=='negative_cash_flow':salary,base=4800,4700
        if scenario=='suspicious_transfer_burst':salary,base=9500,5000
        if scenario=='suspicious_large_transfer':salary,base=14000,3600
        rent=min(2400,base*rng.randint(32,41)//100)
        opening=salary*rng.randint(3,7)*100+rng.randrange(500,4001)*100
        transactions=[]
        def add(month,day,hour,minute,amount,typ,category,recipient,desc):
            transactions.append({'transaction_id':f'{cid}-T{len(transactions)+1:04}','timestamp':f'2026-{month:02}-{day:02}T{hour:02}:{minute:02}:00+00:00','amount':money(amount),'direction':'credit' if typ=='salary' else 'debit','transaction_type':typ,'category':category,'recipient_id':recipient,'description':desc})
        for month in range(1,7):
            target=base*100+rng.randint(-9000,9000)
            if scenario=='increasing_spending':target=[2200,2500,2800,3100,3400,5200][month-1]*100
            if scenario=='negative_cash_flow':target=(5200 if month==6 else 4700)*100+rng.randint(-3000,3000)
            add(month,1,9,0,salary*100,'salary','salary',None,'Monthly payroll credit')
            add(month,3,10,0,rent*100,'purchase','rent',f'{cid}-R01','Residential rent payment')
            transfer_count=rng.choice([1,2])
            transfer_amounts=[30000 if scenario=='suspicious_large_transfer' else rng.randint(18000,32000) for _ in range(transfer_count)]
            for j,a in enumerate(transfer_amounts):
                # Ordinary new recipients occur in a small, isolated payment.
                recipient=f'{cid}-R{2+j:02}'
                if scenario=='ordinary' and month==6 and i%4==0 and j==0:recipient=f'{cid}-R07'
                add(month,7+j*14,12,0,a,'transfer','personal_transfer',recipient,['Family contribution','Shared household payment'][j])
            # Category weights reflect repeatable household habits, with modest variation.
            expense_count=rng.randint(12,16)
            if scenario=='suspicious_transfer_burst' and month==6:expense_count=12
            cats=['groceries','utilities','transport','dining','shopping','groceries','transport','dining','groceries','utilities','shopping','transport']
            if month%2==0:cats[-1]='healthcare'
            while len(cats)<expense_count:cats.append(rng.choice(['groceries','transport','dining','shopping']))
            weights={'groceries':100,'utilities':135,'transport':55,'dining':65,'shopping':95,'healthcare':70}
            weighted=[weights[c]*rng.randint(90,110) for c in cats]
            remaining=target-rent*100-sum(transfer_amounts)
            amounts=[remaining*w//sum(weighted) for w in weighted];amounts[-1]+=remaining-sum(amounts)
            desc={'groceries':'Grocery store purchase','utilities':'Household utility payment','transport':'Local travel payment','dining':'Restaurant purchase','shopping':'Household goods purchase','healthcare':'Pharmacy purchase'}
            for j,(cat,amount) in enumerate(zip(cats,amounts)):
                add(month,4+j,15,j,amount,'purchase',cat,f'{cid}-M{CATEGORIES.index(cat)+1:02}',desc[cat])
            if month==6 and scenario=='expected_furniture_purchase':add(6,16,11,0,600000,'purchase','shopping',f'{cid}-M07','Furniture store purchase')
            if month==6 and scenario=='suspicious_transfer_burst':
                for j in range(5):add(6,18,14,6*j,40000,'transfer','personal_transfer',f'{cid}-R08','Personal payment')
            if month==6 and scenario=='suspicious_large_transfer':add(6,24,16,10,800000,'transfer','personal_transfer',f'{cid}-R08','Personal payment')
        transactions.sort(key=lambda t:t['timestamp'])
        closing=opening+sum(cents(t['amount'])*(1 if t['direction']=='credit' else -1) for t in transactions)
        clients.append({'client_id':cid,'currency':'USD','monthly_income':money(salary*100),'opening_balance':money(opening),'closing_balance':money(closing),'transactions':transactions})
        if cid not in source_map:continue
        row=source_map[cid].copy();goal=row['Financial goals']
        import re
        match=re.fullmatch(r'(.+): \$([\d,]+\.\d{2}) by (\d{4}-\d{2}-\d{2})',goal)
        assert match,goal
        goal_type,amount,deadline=match.groups()
        milestone='contribution milestone' if goal_type=='Retirement savings' else 'savings target'
        first_note=f'2026-02-{10+i%15:02}: Reviewed the {goal_type.lower()} goal ({milestone}) of ${amount} by {deadline}. Account cash is not assumed to be entirely allocated to this goal.'
        ordinary_notes=[
            'Review the household budget and regular contributions at the next meeting.',
            'Client would like to compare regular expenses with progress toward the existing goal.',
            'Client plans a routine review of utilities and transport costs before the next meeting.',
            'Discuss a manageable contribution schedule alongside everyday household spending.',
            'Client asked to keep room in the budget for modest planned household purchases.'
        ]
        second=f'2026-05-{12+i%12:02}: '+ordinary_notes[i%len(ordinary_notes)]
        if scenario=='expected_furniture_purchase':second='2026-05-15: Client expects an approximately $6,000 furniture purchase in June for the apartment. Review how the planned purchase fits alongside regular goal contributions.'
        if scenario=='increasing_spending':second='2026-05-18: Client requested a review of discretionary spending while preserving contributions toward the existing goal.'
        if scenario=='negative_cash_flow':second='2026-05-19: Client would like to review sustained household commitments, budget pressure, and the available savings cushion before adjusting goal contributions.'
        if cid=='C008':second+=' Client said, "Please review travel and savings together."'
        row['Advisor context']=first_note+'\n'+second;updated.append(row)
    return {'schema_version':'2.0','currency':'USD','period_start':'2026-01-01','period_end':'2026-06-30','clients':clients},updated,mapping

def profile(row):
    return {'advisor_context':row['Advisor context']}

def validate(data,source,updated,mapping,expected):
    assert len(data['clients'])==25 and len(updated)==24
    ids={c['client_id'] for c in data['clients']};crmids={r['Client ID'] for r in updated}
    assert ids=={f'C{i:03}' for i in range(1,26)} and ids-crmids=={'C005'}
    assert len(mapping)==5 and set(mapping.values())==set(SCENARIOS)
    assert set(mapping)<=crmids
    assert list(mapping)==random.Random(42).sample([r['Client ID'] for r in source],5)
    for original,changed in zip(source,updated):
        assert all(original[h]==changed[h] for h in HEADERS[:-1])
        assert changed['Financial goals'].split(' by ')[-1] in changed['Advisor context']
        assert 'INR' not in changed['Advisor context']
        for line in changed['Advisor context'].splitlines():assert line[:10]<'2026-06-01'
    pmap={r['Client ID']:profile(r) for r in updated};seen=set();suspicious=set()
    for c,e in zip(data['clients'],expected['clients']):
        assert c['currency']=='USD' and cents(c['monthly_income'])>0
        tx=c['transactions'];assert tx==sorted(tx,key=lambda t:t['timestamp'])
        bal=cents(c['opening_balance']);rents=[]
        for month in range(1,7):
            mt=[t for t in tx if datetime.fromisoformat(t['timestamp']).month==month]
            assert 15<=len(mt)<=25,(c['client_id'],month,len(mt))
            salaries=[t for t in mt if t['transaction_type']=='salary'];assert len(salaries)==1 and salaries[0]['amount']==c['monthly_income']
            rent=[t for t in mt if t['category']=='rent'];assert len(rent)==1;rents.append((rent[0]['amount'],rent[0]['recipient_id']))
        assert len(set(rents))==1
        for t in tx:
            assert t['transaction_id'] not in seen;seen.add(t['transaction_id'])
            dt=datetime.fromisoformat(t['timestamp']);assert dt.utcoffset()==timedelta(0)
            assert '2026-01-01'<=dt.date().isoformat()<='2026-06-30' and 'date' not in t
            assert cents(t['amount'])>0
            assert t['direction'] in ('credit','debit') and t['transaction_type'] in ('salary','purchase','transfer')
            assert t['category'] in CATEGORIES+['salary','rent','personal_transfer']
            if t['direction']=='credit':assert t['transaction_type']=='salary' and t['category']=='salary' and t['recipient_id'] is None
            else:
                assert t['transaction_type']!='salary' and isinstance(t['recipient_id'],str)
                assert (t['transaction_type']=='transfer')==(t['category']=='personal_transfer')
            assert not any(word in str(t).lower() for word in ['scam','fraud','suspicious','scenario','rule_id'])
            bal+=cents(t['amount'])*(1 if t['direction']=='credit' else -1);assert bal>=0
        assert bal==cents(c['closing_balance'])
        actual,_,_=evaluate(c,pmap.get(c['client_id']))
        actual['scenario']=mapping.get(c['client_id'],'ordinary')
        assert actual==e
        assert actual['expected_triggered_rule_ids']==RULES.get(mapping.get(c['client_id']),[]),(c['client_id'],actual)
        if set(actual['expected_triggered_rule_ids'])&{'R4','R5'}:suspicious.add(c['client_id'])
    assert len(suspicious)==2
    furniture_cid=next(cid for cid,s in mapping.items() if s=='expected_furniture_purchase')
    note=next(r['Advisor context'] for r in updated if r['Client ID']==furniture_cid)
    assert '2026-05-15' in note and '$6,000' in note and 'June' in note
    furniture=[(c['client_id'],t) for c in data['clients'] for t in c['transactions'] if t['description']=='Furniture store purchase']
    assert len(furniture)==1 and furniture[0][0]==furniture_cid and furniture[0][1]['amount']==6000 and furniture[0][1]['timestamp'].startswith('2026-06')
    assert '2031-12-31' in next(r['Advisor context'] for r in updated if r['Client ID']=='C003')
    return len(seen)

README='''# Connected USD Wealth Advisor demonstration data

All records are synthetic. This realistic demonstration dataset deliberately inserts five situations to test application behavior; it does not establish real-world fraud detection accuracy. Suspicious patterns require client verification and human review, not a confirmed-fraud conclusion.

## Run

Python 3.9+, standard library only:

`python generate_dataset.py --source source_contacts.csv --output-dir generated`

Run without `-O`, because assertions implement validation. source_contacts.csv is the exact supplied starting file, preserved for reproducibility and identity comparison. It retains the obsolete original note and is NOT an application input. Use only the final updated contacts file for CRM import. Currency conversion is not performed: USD incomes and transactions are generated directly. Existing dollar goal strings are preserved exactly.

## Inputs for the application

financial_data.json: schema_version, USD currency, period bounds, clients array. Each of 25 clients has client_id, currency, expected recurring monthly_income, opening_balance, closing_balance, transactions. Each transaction has globally unique ID, explicit UTC timestamp, positive numeric amount, direction, type, category, recipient and ordinary description. No dates duplicated or scenario labels added.

hubspot_contacts_updated.csv: 24 contacts, C005 deliberately absent. Join Client ID to financial client_id. Names, email, risk and complete goal strings are unchanged. No transaction history belongs in the CRM import.

Only these two files are production inputs. expected_findings.json contains scenario assignments, calculated evidence and required warnings; it is exclusively for evaluation and must never be passed to agents. validation_summary.csv contains actual recomputed metrics and rule IDs, and is also evaluation-only. No agents or architecture are implemented here.

## Accounting and realism

Integer cents/Decimal arithmetic avoids binary accounting errors. JSON amounts are numbers with at most two decimal places; CSV goals use $. Every credit is salary, once monthly and consistent. Rent recurs at a consistent amount and recipient. All debits, including personal transfers, count as outgoing spending. There are no refunds, loans or own-account transfers. Closing = opening + credits - debits; all running balances are nonnegative. UTC is a dataset convention, not a customer-location claim.

Normal customers vary in salary, cash reserves, regular spending, category habits and monthly counts. Some ordinary customers pay a new recipient once. Goals retain their original target amounts and deadlines. Cash balances are not represented as fully earmarked; retirement targets are contribution milestones, not sufficient retirement wealth. No holdings, debts or ages are invented.

## June demonstration rules

R1: individual debit strictly above 50% of actual June salary credits; do not evaluate when salary is unavailable or zero.
R2: June total debits strictly above 1.5 times March-May mean debits, requiring all three months.
R3: June debits exceed actual June salary credits.
R4: five or more outgoing transfers to one new recipient within an inclusive 30-minute window, with no earlier transfer to that recipient before the first window payment.
R5: transfer to a new recipient strictly above both $5,000 and five times the January-May median outgoing transfer; require five historical transfers.

New means unseen within the observed history, not proof of a first-ever payment. R1 and R5 have separate thresholds and can identify the same transaction while R2 records its monthly impact. Overlapping rules are related evidence, not separate incidents. These thresholds are demo assumptions, not universal financial or fraud rules. Evidence averages retain Decimal precision; displayed summary averages round to cents.

## Deterministic assignment

Before generating financial records, random.Random(42).sample selects five distinct IDs from the source CSV row order. They receive furniture, increasing spending, negative cash flow, transfer burst and large transfer in that order. A separate RNG generates all financial variation. Source row order is therefore part of the reproducibility contract. The remaining 19 CRM customers and C005 are ordinary controls.

## CRM edits

All advisor notes are refreshed with individualized pre-June dates, each existing goal's exact target date, and natural discussion topics. The old C002 INR furniture note is removed. C003 now consistently refers to 2031-12-31. The selected furniture customer gets a May note anticipating approximately $6,000 in June; this is a possible explanation, not authorization proof. Increasing-spending and budget-pressure customers have appropriate review notes. The two suspicious-transfer customers receive ordinary notes without explanations of those specific payments. Final inputs contain no INR references, rule IDs, or test scenario labels.

## HubSpot mapping

| CSV header | Property | Suggested field type |
|---|---|---|
| First name | Standard firstname | Single-line text |
| Last name | Standard lastname | Single-line text |
| Email | Standard email | Standard email |
| Client ID | Custom wealth_client_id | Single-line text |
| Risk tolerance | Custom wealth_risk_tolerance | Dropdown: low, medium, high |
| Financial goals | Custom wealth_financial_goals | Multi-line text |
| Advisor context | Custom wealth_advisor_context | Multi-line text |

Four custom Contact properties only. These are proposed mappings, not statements about current account entitlements. Map each column explicitly when importing and confirm 24 contacts. CSV escaping preserves multiline notes, commas and quotation marks. No live HubSpot calls are made.

## Validation

The detector reads actual transactions without consulting scenario assignments or generation totals. Assertions check scenario matches, counts, unique IDs, positive cents, timestamps, categories, salary/rent recurrence, balances, exactly two suspicious clients, original CRM values, CSV round trip, furniture note alignment, goal deadlines and absence of label leakage. expected_findings evidence is calculated rather than hardcoded. The summary labels derive from the detector, never from assignment labels.
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default=str(Path(__file__).with_name('source_contacts.csv')))
    parser.add_argument('--output-dir',default='generated')
    args=parser.parse_args();out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)
    source=read_source(Path(args.source));data,updated,mapping=generate(source)
    pmap={r['Client ID']:profile(r) for r in updated}
    results=[];summary=[]
    for c in data['clients']:
        e,income,total=evaluate(c,pmap.get(c['client_id']));e['scenario']=mapping.get(c['client_id'],'ordinary');results.append(e)
        baseline=sum(cents(t['amount']) for t in c['transactions'] if t['direction']=='debit' and datetime.fromisoformat(t['timestamp']).month in (3,4,5))
        summary.append({'client_id':c['client_id'],'transaction_count':len(c['transactions']),'june_income':f'{money(income):.2f}','r1_50pct_salary_threshold':f'{Decimal(income)/200:.2f}','march_may_average_spending':f'{Decimal(baseline)/300:.2f}','june_spending':f'{money(total):.2f}','june_surplus_or_deficit':f'{money(income-total):.2f}','triggered_rule_ids':', '.join(e['expected_triggered_rule_ids']) or 'None','crm_available':'Yes' if c['client_id'] in pmap else 'No'})
    expected={'schema_version':'3.0','currency':'USD','evaluation_month':'2026-06','agent_input':False,'scenario_assignment':mapping,'expected_monthly_r1':derive_monthly_r1(data),'clients':results}
    write_json(out/'financial_data.json',data);write_json(out/'expected_findings.json',expected)
    with (out/'hubspot_contacts_updated.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=HEADERS);w.writeheader();w.writerows(updated)
    with (out/'validation_summary.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    readback=read_source(out/'hubspot_contacts_updated.csv');assert readback==updated
    count=validate(load_json(out/'financial_data.json'),source,readback,mapping,load_json(out/'expected_findings.json'))
    prod=(out/'financial_data.json').read_text()+(out/'hubspot_contacts_updated.csv').read_text()
    assert 'INR' not in prod
    for label in SCENARIOS:assert label not in prod
    assert not any(f'"{r}"' in prod for r in ['R1','R2','R3','R4','R5'])
    table='\n## Scenario-to-client mapping (test only)\n\n| Client | Scenario | Rules |\n|---|---|---|\n'+'\n'.join(f"| {cid} | {s.replace('_',' ')} | {', '.join(RULES[s])} |" for cid,s in mapping.items())
    (out/'DATASET_README.md').write_text(README+table+'\n',encoding='utf-8')
    write_json(out/'validation_report.json',{'status':'passed','seed':42,'financial_customers':25,'crm_profiles':24,'transactions':count,'special_scenarios':5,'suspicious_customers':2,'sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.name in ['financial_data.json','hubspot_contacts_updated.csv','expected_findings.json','validation_summary.csv','DATASET_README.md']}})
    print('PASS:',count,'transactions; all identities/goals preserved; every scenario matches.')
    print('Assignment:',mapping)
    for r in summary:print(' | '.join(str(v) for v in r.values()))

if __name__=='__main__':main()
