import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from bs4 import BeautifulSoup

import monitor
import prospectus
from registry import SourceError, TBILISI, bond_counts, document_url, parse_rows

FIXTURE = Path(__file__).parent / 'fixtures/nbg-2026-09-14.html'
NOW = datetime(2026, 9, 14, 12, tzinfo=TBILISI)


def source():
    return parse_rows(FIXTURE.read_text())


def baseline():
    return monitor.new_state(source(), NOW)


def kinds(state):
    return [e['kind'] for e in state['log']]


def test_real_nbg_snapshot():
    rows = source()
    assert bond_counts(rows) == dict(bonds=47, issuers=22, assigned=46, awaiting_isin=1, shares=10)
    assert rows['nbg:645']['issuer'] == 'შპს მიკროსაფინანსო ორგანიზაცია რიკო ექსპრესი'
    redix = rows['nbg:642']
    assert redix['id'] == '404890891'
    assert redix['isin'] == 'GE2700605613'
    assert 'usd2008.pdf' in redix['documents'][0]
    assert rows['nbg:385']['date'] == '2000-01-01'


def test_whole_page_noise_and_cache_busters_do_not_create_events():
    html = FIXTURE.read_text().replace('?v=', '?v=other')
    html = '<style>.jsx-675588824 {}</style><p>01.01.2020 website updated</p>' + html
    state, _ = monitor.reconcile(baseline(), parse_rows(html), NOW+timedelta(hours=4))
    assert kinds(state) == []


def test_ignore_dates_names_and_website_noise():
    rows = source()
    rows['nbg:642'].update(date='2026-09-14', site='https://example.org', issuer='შპს ჭავჭავაძის 64ბ  ')
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == []


def test_document_url_preserves_meaningful_parameters():
    assert document_url('/fm/test.pdf?v=a#page=4') == 'https://nbg.gov.ge/fm/test.pdf'
    assert document_url('/fm/test.pdf?document=12&v=a') != document_url('/fm/test.pdf?document=13&v=b')
    assert document_url('/fm/%E1%83%90.pdf') == document_url('/fm/ა.pdf')


@pytest.mark.parametrize('url', ['http://nbg.gov.ge/x.pdf', 'https://example.org/x.pdf', '/fm/a.exe'])
def test_unexpected_document_origin_rejected(url):
    with pytest.raises(SourceError):
        document_url(url)


def test_missing_or_partial_source_never_accepted():
    for html in ['<html>maintenance 200</html>', FIXTURE.read_text().replace('class="tb-row"', 'class="gone"', 1)]:
        with pytest.raises(SourceError):
            parse_rows(html)


def test_dataset_column_mismatch_rejected():
    html = FIXTURE.read_text().replace('GE 2700605613</a>', 'GE 2700605614</a>')
    with pytest.raises(SourceError):
        parse_rows(html)


def test_silent_legacy_migration():
    state, _ = monitor.reconcile({'v':5, 'rows':{'bogus':{'issuer':'filename.pdf'}}}, source(), NOW)
    assert state['v'] == 6 and not state['log'] and not state['outbox']
    assert all(item['status'] == 'baseline' for item in state['documents'].values())


def test_isin_assignment_is_one_event_and_no_removal():
    state = baseline()
    rows = source()
    rows['nbg:645']['isin'] = 'GE2700605999'
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=4))
    assert kinds(state) == ['isin_assigned']
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=8))
    assert kinds(state) == ['isin_assigned']


def test_nbg_record_recreated_without_false_publication():
    rows = source()
    rows['nbg:9999'] = rows.pop('nbg:645')
    rows['nbg:9999']['source_id'] = '9999'
    rows['nbg:9999']['isin'] = 'GE2700605999'
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == ['isin_assigned']


def test_new_prospectus_without_isin_queues_extraction():
    rows = source()
    new = copy.deepcopy(rows['nbg:645'])
    new.update(source_id='1000', documents=['https://nbg.gov.ge/fm/new.pdf'])
    rows['nbg:1000'] = new
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == ['prospectus_added']
    assert state['documents'][new['documents'][0]]['status'] == 'pending'


def test_share_publications_do_not_trigger_bond_alerts():
    rows = source()
    rows['nbg:385']['documents'] = ['https://nbg.gov.ge/fm/share.pdf']
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == []


def test_replacement_not_add_remove_pair():
    rows = source()
    rows['nbg:645']['documents'] = ['https://nbg.gov.ge/fm/rico-final.pdf']
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == ['documents_replaced']
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=4))
    assert kinds(state) == ['documents_replaced']


def test_document_removal_requires_two_checks():
    rows = source()
    rows['nbg:645']['documents'] = []
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert not kinds(state)
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=4))
    assert kinds(state) == ['documents_removed']


def test_second_document_on_same_row_detected():
    rows = source()
    rows['nbg:645']['documents'].append('https://nbg.gov.ge/fm/supplement.pdf')
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == ['prospectus_added']


def test_removal_and_reappearance_do_not_alert():
    rows = source()
    del rows['nbg:645']
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert not kinds(state)
    state, _ = monitor.reconcile(state, source(), NOW+timedelta(hours=4))
    assert not kinds(state) and not state['pending_removals']


def test_confirmed_removal_is_once_only():
    rows = source()
    del rows['nbg:645']
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=4))
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=8))
    assert kinds(state) == ['bond_removed']


def test_failure_preserves_rows_and_interrupts_confirmation():
    rows = source()
    del rows['nbg:645']
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    before = copy.deepcopy(state['rows'])
    monitor.record_failure(state)
    assert state['rows'] == before
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=4))
    assert not kinds(state)


def test_large_drop_quarantined_but_real_removals_can_be_confirmed():
    state = baseline()
    rows = dict(list(source().items())[:20])
    state, _ = monitor.reconcile(state, rows, NOW)
    assert not kinds(state) and len(state['observed_rows']) == 57
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=4))
    assert not kinds(state) and len(state['observed_rows']) == 20
    state, _ = monitor.reconcile(state, rows, NOW+timedelta(hours=8))
    assert 'bond_removed' in kinds(state)


def test_html_escaping_and_chunk_limits():
    state = baseline()
    e = {'kind':'bond_added', 'row':dict(source()['nbg:645'], issuer='Issuer <B> & Co'), 'documents':[]}
    text = ''.join(monitor.format_alert(e, state))
    assert 'Issuer &lt;B&gt; &amp; Co' in text
    chunks = monitor.chunk_lines(['🆕' * 1000] * 4)
    assert all(monitor.units(c) <= 3800 for c in chunks)


def test_failed_delivery_remains_queued_and_retries(tmp_path):
    state = baseline()
    monitor.event(state,'bond_added',source()['nbg:645'],NOW)
    path = tmp_path/'state.json'
    with pytest.raises(RuntimeError):
        monitor.flush_outbox(state,path,sender=Mock(side_effect=RuntimeError('failed')))
    assert len(monitor.load_state(path)['outbox']) == 1
    sent = []
    monitor.flush_outbox(state,path,sender=sent.append)
    assert len(sent) == 1 and not state['outbox']


def test_digest_covers_post_21h_changes_and_is_idempotent(tmp_path):
    state = baseline()
    state['last_digest_at'] = datetime(2026,9,13,21,tzinfo=TBILISI).isoformat()
    ts = datetime(2026,9,13,22,tzinfo=TBILISI)
    monitor.event(state,'bond_added',source()['nbg:645'],ts)
    sent = []
    monitor.run_digest(state,NOW,tmp_path/'state.json',sender=sent.append)
    assert 'Bond entry added: 1' in ''.join(sent)
    monitor.run_digest(state,NOW,tmp_path/'state.json',sender=sent.append)
    assert len(sent) == 1


def test_digest_is_offline_and_excludes_shares(tmp_path):
    with patch('requests.get', side_effect=AssertionError('must be offline')):
        msgs, counts = monitor.digest_messages(baseline(), NOW)
    assert counts['bonds'] == 47 and counts['shares'] == 10
    assert 'Minor updates' not in ''.join(msgs)


def test_corrupt_state_not_silently_reset(tmp_path):
    path = tmp_path/'state.json'
    path.write_text('{broken')
    with pytest.raises(ValueError):
        monitor.load_state(path)


def test_existing_document_link_on_new_entry_is_not_new_publication():
    rows = source()
    rows['nbg:9999'] = dict(rows['nbg:645'], source_id='9999', isin='GE2700605999')
    state, _ = monitor.reconcile(baseline(), rows, NOW)
    assert kinds(state) == ['bond_added']


def test_delayed_pdf_followup_only_after_initial_announcement():
    state = baseline()
    state['documents']['https://nbg.gov.ge/fm/new.pdf'] = {'status':'retry', 'announced':True}
    prospectus.enrich(state,NOW,fetcher=lambda url:{'status':'ready','terms':{}})
    assert state['documents']['https://nbg.gov.ge/fm/new.pdf']['followup_due'] is True


def test_digest_retries_only_undelivered_chunks(tmp_path):
    state = baseline()
    # Force a multi-part digest with real event rendering and links.
    for i in range(55):
        monitor.event(state,'prospectus_added',source()['nbg:645'],NOW+timedelta(seconds=i+1),['https://nbg.gov.ge/fm/new.pdf'])
    delivered=[]
    def fail_second(message):
        if delivered:
            raise RuntimeError('delivery failure')
        delivered.append(message)
    later=NOW+timedelta(hours=4)
    with pytest.raises(RuntimeError):
        monitor.run_digest(state,later,tmp_path/'state.json',sender=fail_second)
    assert state['last_digest_at'] == NOW.isoformat()
    first=delivered[0]
    monitor.run_digest(state,later,tmp_path/'state.json',sender=delivered.append)
    assert delivered.count(first) == 1
    assert state['last_digest_at'] == later.isoformat()


def test_dry_run_does_not_write_send_or_download(tmp_path):
    statepath=tmp_path/'state.json'
    statepath.write_text(json.dumps(baseline()))
    original=statepath.read_bytes()
    with patch('sys.argv',['monitor.py','--dry-run','--html',str(FIXTURE),'--state',str(statepath)]), \
         patch('requests.get',side_effect=AssertionError('unexpected network')), \
         patch('requests.post',side_effect=AssertionError('unexpected message')):
        monitor.main()
    assert statepath.read_bytes()==original
