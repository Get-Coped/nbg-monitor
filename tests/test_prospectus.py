from datetime import timedelta

from pypdf import PdfWriter
import io

import prospectus
from test_monitor import baseline, NOW


def test_indicative_fixed_terms_and_restrictions():
    pages = [
        'წინასწარი დოკუმენტი. 10,000,000 (ათი მილიონი) აშშ დოლარამდე ჯამური ნომინალური ღირებულების ფიქსირებული ობლიგაციები. გამოშვებიდან 36 თვის შემდეგ. განთავსების აგენტი: თიბისი კაპიტალი.',
        'ობლიგაციების საპროცენტო განაკვეთი (კუპონი) შეადგენს წლიურად [7.00%-7.25%]. პროცენტი გადაიხდება წელიწადში ორჯერ. თითოეული ინვესტორის ინვესტიცია შეადგენს მინიმუმ 500,000 ლარის ექვივალენტს აშშ დოლარში. აკრძალულია ობლიგაციების თანასაკუთრება.'
    ]
    result = prospectus.extract_terms(pages)
    f = result['fields']
    assert f['Currency and amount']['value'] == 'Up to USD 10,000,000'
    assert f['Coupon']['value'] == 'Fixed: 7.00%-7.25%'
    assert f['Placement agent']['value'] == 'TBC Capital'
    assert f['Tenor']['value'] == '36 months'
    assert f['Coupon payments']['value'] == 'Semiannual'
    assert result['restrictions']
    assert all(x['page'] == 2 for x in result['restrictions'])


def test_floating_coupon_keeps_base_and_range():
    result = prospectus.extract_terms(['წინასწარი დოკუმენტი. საპროცენტო განაკვეთი: TIBR3M + 3.50%-3.75%. საპროცენტო სარგებლის გადახდა კვარტალური.'])
    assert result['fields']['Coupon']['value'] == 'Floating: TIBR3M + 3.50%-3.75%'
    assert result['fields']['Coupon payments']['value'] == 'Quarterly'


def test_no_claimed_coupon_from_financial_ratio_or_call_price():
    result = prospectus.extract_terms(['Issuer has net margin 6.5%. Call price 100.50%.'])
    assert 'Coupon' not in result['fields']


def test_offer_does_not_use_minimum_investment_as_issue_size():
    result = prospectus.extract_terms(['Minimum investment USD 500,000. ნომინალური ღირებულება 1,000 აშშ დოლარი.'])
    assert 'Currency and amount' not in result['fields']


def test_multiple_tranches_are_not_silently_collapsed():
    result = prospectus.extract_terms(['USD issuance: 10,000,000 USD aggregate nominal amount. EUR issuance: 5,000,000 EUR aggregate nominal amount.'])
    assert 'Multiple amounts' in result['fields']['Currency and amount']['value']


def test_empty_pdf_is_explicit_review_not_fake_terms():
    pdf = PdfWriter()
    pdf.add_blank_page(width=500,height=500)
    stream = io.BytesIO()
    pdf.write(stream)
    result = prospectus.read_pdf(stream.getvalue())
    assert result['status'] == 'needs_review'


def test_extraction_budget_cache_and_delayed_retry():
    state = baseline()
    for i in range(3):
        state['documents'][f'https://nbg.gov.ge/fm/new{i}.pdf'] = {'status':'pending'}
    calls = []
    def fetcher(url):
        calls.append(url)
        return {'status':'ready','terms':{}}
    assert prospectus.enrich(state,NOW,fetcher=fetcher) == 2
    assert len(calls) == 2
    assert prospectus.enrich(state,NOW,fetcher=fetcher) == 1
    assert prospectus.enrich(state,NOW+timedelta(hours=4),fetcher=fetcher) == 0


def test_failed_extraction_retains_publication_and_caps_retries():
    state = baseline()
    url = 'https://nbg.gov.ge/fm/fail.pdf'
    state['documents'][url] = {'status':'pending'}
    def fail(url):
        raise ValueError('unreadable')
    for offset in [0,4,12,24,36]:
        prospectus.enrich(state,NOW+timedelta(hours=offset),fetcher=fail)
    assert state['documents'][url]['attempts'] == 3
    assert state['documents'][url]['status'] == 'needs_review'


def test_final_terms_heading_not_overruled_by_preliminary_boilerplate():
    result = prospectus.extract_terms(['ობლიგაციების საბოლოო შეთავაზების პირობების დოკუმენტი. ' + 'დამატებითი ინფორმაცია. '*30 + 'წინასწარი პროსპექტის დამტკიცებიდან 12 თვე'])
    assert result['stage'] == 'Final terms / prospectus'


def test_preliminary_title_not_overruled_by_reference_to_future_final_terms():
    result=prospectus.extract_terms(['ობლიგაციების წინასწარი შეთავაზების პირობების დოკუმენტი. საბოლოო პირობების დოკუმენტი წარედგინება მოგვიანებით.'])
    assert result['stage'] == 'Preliminary / indicative'


def test_single_percent_sign_preserves_indicative_range_and_issue_date():
    result = prospectus.extract_terms(['Preliminary offering terms. Fixed coupon rate 6.5–7.0%. Indicative issue date: 17 September 2026.'])
    assert result['fields']['Coupon']['value'] == 'Fixed: 6.5–7.0%'
    assert result['fields']['Issue date']['value'] == '17 September 2026'
    assert result['document_type'] == 'Offering terms'


def test_programme_ceiling_is_not_reported_as_tranche_amount():
    result = prospectus.extract_terms(['Programme prospectus. 50,000,000 USD aggregate nominal amount. Fixed coupon rate 7%.'])
    assert 'Currency and amount' not in result['fields']
    assert 'Coupon' not in result['fields']
    assert result['fields']['Programme amount']['value'] == 'USD 50,000,000'


def test_preliminary_retries_get_priority_but_keep_global_budget():
    state = {'documents': {}}
    for url, row in [('final', {'kind':'bond','isin':'GE1234567890'}), ('preliminary', {'kind':'bond','isin':''})]:
        state['documents'][url] = {'status':'retry', 'attempts':1, 'last_attempt':NOW.isoformat(), 'row':row}
    calls=[]
    def fetch(url):
        calls.append(url)
        return {'status':'ready','terms':{}}
    assert prospectus.enrich(state,NOW+timedelta(minutes=14),fetcher=fetch,limit=1) == 0
    assert prospectus.enrich(state,NOW+timedelta(minutes=15),fetcher=fetch,limit=1) == 1
    assert calls == ['preliminary']
    assert state['documents']['final']['attempts'] == 1
