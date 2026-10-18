from __future__ import annotations
import argparse, asyncio, json, re, time
from pathlib import Path
from playwright.async_api import async_playwright

HOME='https://www.swiggy.com/instamart'
def ms(): return time.perf_counter()*1000
def redact(h):
    return {k:('<REDACTED>' if k.lower() in {'cookie','authorization','set-cookie','x-access-token'} else v) for k,v in h.items()}
def productish(x):
    if isinstance(x,dict):
        n=x.get('name') or x.get('displayName') or x.get('productName')
        if isinstance(n,str) and any(k in x for k in ('price','finalPrice','sellingPrice','offerPrice','defaultPrice')): return 1
        return sum(productish(v) for v in x.values())
    if isinstance(x,list): return sum(productish(v) for v in x)
    return 0

def extract_product_like(node, out, seen):
    if isinstance(node, dict):
        name = node.get("name") or node.get("displayName") or node.get("productName") or node.get("itemName")
        pid = node.get("productId") or node.get("productID") or node.get("itemId") or node.get("spinId") or node.get("id")
        price_keys = ("price", "finalPrice", "sellingPrice", "offerPrice", "defaultPrice", "mrp")
        if isinstance(name, str) and name.strip() and any(k in node for k in price_keys):
            key = str(pid).strip() if pid is not None else name.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(node)
        for v in node.values():
            extract_product_like(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            extract_product_like(v, out, seen)

def extract_products(body):
    out=[]; extract_product_like(body,out,set()); return out


async def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--lat',type=float,required=True); ap.add_argument('--lon',type=float,required=True); ap.add_argument('--query',nargs='+',default=['milk']); args=ap.parse_args()
    qs=[q for q in args.query if q.strip()]
    out=Path('instamart_probe_v7')/time.strftime('%Y%m%d-%H%M%S'); out.mkdir(parents=True,exist_ok=True)
    report={'coords':{'lat':args.lat,'lon':args.lon},'queries':qs,'responses':[],'requests':[],'search_bundles':[]}
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=False,args=['--disable-blink-features=AutomationControlled','--disable-dev-shm-usage'])
        context=await browser.new_context(viewport={'width':1440,'height':900},geolocation={'latitude':args.lat,'longitude':args.lon,'accuracy':10},permissions=['geolocation'],locale='en-IN',timezone_id='Asia/Kolkata')
        page=await context.new_page()

        # Deterministic geolocation injection for the Swiggy frontend.
        # Playwright context geolocation is also enabled above; this override
        # guarantees that a site-side navigator.geolocation call sees exactly
        # the coordinates supplied to this probe.
        await page.add_init_script('''(coords => {
            const original = navigator.geolocation;
            const position = {
                coords: {
                    latitude: coords.lat,
                    longitude: coords.lon,
                    accuracy: 10,
                    altitude: null,
                    altitudeAccuracy: null,
                    heading: null,
                    speed: null
                },
                timestamp: Date.now()
            };
            Object.defineProperty(navigator, 'geolocation', {
                configurable: true,
                value: {
                    getCurrentPosition(success, error) {
                        setTimeout(() => success(position), 0);
                    },
                    watchPosition(success, error) {
                        setTimeout(() => success(position), 0);
                        return 1;
                    },
                    clearWatch() {}
                }
            });
        })(''' + json.dumps({'lat':args.lat,'lon':args.lon}) + ''');''')

        async def on_request(r):
            u=r.url
            interesting=('swiggy.com/api/instamart' in u or 'instamart-search' in u.lower() or ('/search' in u.lower() and 'swiggy.com' in u))
            if not interesting: return
            try: h=redact(await r.all_headers())
            except: h={}
            rec={'method':r.method,'url':u,'headers':h}
            try:
                b=r.post_data
                if b: rec['post_data']=b[:20000]
            except: pass
            report['requests'].append(rec); print('\n[REQUEST]',r.method,u)
            if r.post_data: print('POST:',r.post_data[:2000])
        async def on_response(r):
            u=r.url
            if 'swiggy.com/api/instamart' not in u and 'instamart-search' not in u.lower(): return
            rec={'status':r.status,'url':u}
            try: headers=await r.all_headers(); rec['headers']=redact(headers); ct=headers.get('content-type','')
            except: ct=''
            try:
                if 'json' in ct.lower():
                    body=await r.json(); rec['product_count']=productish(body)
                    if 'api/instamart' in u:
                        fn='response_%03d.json'%len(report['responses']); (out/fn).write_text(json.dumps(body,indent=2,ensure_ascii=False,default=str),encoding='utf8'); rec['body_file']=fn
                elif 'javascript' in ct.lower() or u.lower().endswith('.js'):
                    txt=await r.text()
                    if 'instamart-search' in u.lower() or ('search' in txt.lower() and len(txt)>10000):
                        fn='bundle_%03d.js'%len(report['search_bundles']); (out/fn).write_text(txt,encoding='utf8'); report['search_bundles'].append({'url':u,'file':fn,'size':len(txt)}); print('[BUNDLE SAVED]',u,'size=',len(txt))
            except Exception as e: rec['body_error']=str(e)
            report['responses'].append(rec); print('[RESPONSE]',r.status,u)
        page.on('request',on_request); page.on('response',on_response)
        t=ms()
        # IMPORTANT: do not put lat/lng in the URL here.  This probe intentionally
        # tests the same user-facing location flow we want in production:
        # open Instamart -> Choose location -> Use current location.
        url=HOME
        print('Opening',url)
        await page.goto(url,wait_until='domcontentloaded',timeout=60000)
        await page.wait_for_timeout(5000)
        print('Initial home load ms:',round(ms()-t,1))

        async def dump_location_state(label):
            try:
                data = await page.evaluate("""() => ({
                    url: location.href,
                    text: document.body.innerText.slice(0,18000),
                    buttons: [...document.querySelectorAll('button,[role=button],a')].map((x,i) => ({
                        i,
                        text:(x.innerText||x.textContent||'').trim().slice(0,300),
                        aria:x.getAttribute('aria-label'),
                        title:x.getAttribute('title'),
                        testid:x.getAttribute('data-testid'),
                        href:x.getAttribute('href')
                    })).filter(x => x.text || x.aria || x.title || x.testid)
                })""")
                (out/f'{label}_location_state.json').write_text(
                    json.dumps(data,indent=2,ensure_ascii=False),encoding='utf8')
                try:
                    await page.screenshot(path=str(out/f'{label}.png'), full_page=True)
                except Exception:
                    pass
                relevant = [
                    x for x in data['buttons']
                    if re.search(r'location|deliver|address|current|gps|detect',
                                 json.dumps(x), re.I)
                ]
                print(f'[{label}] location-relevant controls:', relevant[:50])
                return data
            except Exception as e:
                print('LOCATION STATE ERROR:',e)
                return {}

        await dump_location_state('startup_location_popup')

        # The screenshot/user observation shows that the initial Instamart page
        # already contains the location modal with an exact "Use current
        # location" control. Use that control directly.
        current_candidates = page.get_by_text("Use current location", exact=True)
        clicked_current = False
        for i in range(await current_candidates.count()):
            try:
                loc = current_candidates.nth(i)
                if await loc.is_visible():
                    print('CLICK STARTUP USE CURRENT LOCATION: match', i)
                    await loc.click(timeout=5000)
                    clicked_current = True
                    break
            except Exception as e:
                print('USE CURRENT LOCATION CLICK ERROR:', e)

        if not clicked_current:
            print('FATAL: Startup "Use current location" control not found.')
            report['location_flow'] = {
                'startup_popup_present': False,
                'clicked_current_location': False
            }
            await dump_location_state('startup_location_popup_missing')
            await browser.close()
            (out/'report.json').write_text(
                json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding='utf8')
            return

        # Verify browser geolocation independently. The context is configured
        # with the requested coordinates and an init-script override below makes
        # the browser API deterministic for the probe.
        geo = None
        for attempt in range(5):
            try:
                geo = await page.evaluate("""() => new Promise(resolve => {
                    navigator.geolocation.getCurrentPosition(
                        p => resolve({
                            latitude:p.coords.latitude,
                            longitude:p.coords.longitude,
                            accuracy:p.coords.accuracy
                        }),
                        e => resolve({error:e.code,message:e.message}),
                        {enableHighAccuracy:true,timeout:5000,maximumAge:0}
                    )
                })""")
            except Exception as e:
                geo = {'error':str(e)}

            print(f'Geolocation attempt {attempt+1}:', geo)

            if isinstance(geo, dict) and 'latitude' in geo and 'longitude' in geo:
                break
            await page.wait_for_timeout(500)

        report['browser_geolocation'] = geo
        report['location_flow'] = {
            'startup_popup_present': True,
            'clicked_current_location': True,
            'url_after_location': page.url,
            'requested_coordinates': {'lat':args.lat,'lon':args.lon},
            'geolocation_result': geo
        }

        valid_geo = (
            isinstance(geo, dict)
            and isinstance(geo.get('latitude'), (int,float))
            and isinstance(geo.get('longitude'), (int,float))
            and abs(float(geo['latitude']) - args.lat) < 0.0001
            and abs(float(geo['longitude']) - args.lon) < 0.0001
        )

        if not valid_geo:
            print('FATAL: Browser geolocation did not resolve to requested coordinates.')
            print('Requested:', args.lat, args.lon)
            print('Observed :', geo)
            await dump_location_state('location_failed')
            report['VALID_LOCATION_FLOW_TEST'] = False
            (out/'report.json').write_text(
                json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding='utf8')
            await browser.close()
            return

        await page.wait_for_timeout(3000)
        await dump_location_state('after_startup_current_location')

        report['VALID_LOCATION_FLOW_TEST'] = True
        print('VALID LOCATION FLOW: browser coordinates match requested coordinates.')

        # Capture the store ID from the address-selected home API traffic.
        store_id = None
        for rec in reversed(report.get('requests', [])):
            u = rec.get('url','')
            if '/api/instamart/home/v2?' in u:
                m = re.search(r'(?:^|[?&])storeId=([^&]+)', u)
                if m:
                    store_id = m.group(1)
                    break

        report['resolved_store_id'] = store_id
        print('Resolved store ID:', store_id)

        if not store_id:
            print('FATAL: No store ID found after location selection.')
            await browser.close()
            (out/'report.json').write_text(
                json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding='utf8')
            return

        # Let Swiggy settle its location/store state before the direct search.
        await page.wait_for_timeout(2500)

        async def direct_search(query, offset, page_type='INSTAMART_SEARCH_PAGE'):
            payload = {
                'facets': [],
                'sortAttribute': '',
                'query': query,
                'search_results_offset': str(offset),
                'page_type': page_type,
                'is_pre_search_tag': False,
            }
            url = (
                'https://www.swiggy.com/api/instamart/search/v2'
                f'?offset={offset}&ageConsent=false&layoutId=4987'
                '&voiceSearchTrackingId='
                f'&storeId={store_id}&primaryStoreId={store_id}&secondaryStoreId='
            )
            t0 = ms()
            result = await page.evaluate("""async ({url,payload}) => {
                const r = await fetch(url, {
                    method:'POST',
                    credentials:'include',
                    headers:{
                        'accept':'application/json, text/plain, */*',
                        'content-type':'application/json'
                    },
                    body:JSON.stringify(payload)
                });
                const text=await r.text();
                let body;
                try { body=JSON.parse(text); } catch (_) { body=text; }
                return {status:r.status, statusText:r.statusText, body};
            }""", {'url':url,'payload':payload})
            elapsed = round(ms()-t0,1)
            body = result.get('body')
            prods = extract_products(body) if isinstance(body,(dict,list)) else []
            return {
                'query':query,'offset':offset,'page_type':page_type,
                'url':url,'payload':payload,'status':result.get('status'),
                'elapsed_ms':elapsed,'product_count':len(prods),
                'products':prods,'body':body
            }

        direct_results=[]
        for q in qs:
            # Test first page with SEARCH_PAGE, then offset 40 to determine
            # whether pagination is supported by offset.
            for off in (0,40):
                try:
                    rr=await direct_search(q,off,'INSTAMART_SEARCH_PAGE')
                    direct_results.append(rr)
                    fn=f"direct_{re.sub(r'[^a-zA-Z0-9_-]+','_',q)}_{off}.json"
                    (out/fn).write_text(
                        json.dumps(rr,indent=2,ensure_ascii=False,default=str),
                        encoding='utf8')
                    print(f'DIRECT SEARCH {q!r} offset={off}: status={rr["status"]} products={rr["product_count"]} elapsed={rr["elapsed_ms"]} ms')
                except Exception as e:
                    print('DIRECT SEARCH ERROR',q,off,e)

        report['direct_search_results'] = [
            {k:v for k,v in r.items() if k not in ('body','products')}
            for r in direct_results
        ]

        # Give the original UI-search section a chance to capture the exact
        # frontend request as a reference, but the direct results above are the
        # important production-path test.
        # Give the location/store APIs time to settle before searching.
        await page.wait_for_timeout(4000)
        print('Post-location elapsed ms:',round(ms()-t,1))
        dom=await page.evaluate('''() => ({url:location.href,title:document.title,inputs:[...document.querySelectorAll('input,textarea')].map(x=>({tag:x.tagName,placeholder:x.placeholder,aria:x.getAttribute('aria-label'),type:x.type})),buttons:[...document.querySelectorAll('button,[role=button]')].map(x=>({text:(x.innerText||x.textContent||'').trim().slice(0,200),aria:x.getAttribute('aria-label'),title:x.getAttribute('title')})).filter(x=>x.text||x.aria||x.title).slice(0,100),text:document.body.innerText.slice(0,12000)})''')
        (out/'dom_home.json').write_text(json.dumps(dom,indent=2,ensure_ascii=False),encoding='utf8'); print('Inputs:',dom['inputs']); print('Search-ish buttons:',[x for x in dom['buttons'] if 'search' in str(x).lower()][:20])
        # First try actual visible search controls.
        for sel in ['[aria-label*="search" i]','[data-testid*="search" i]','button:has-text("Search")','[role="button"]:has-text("Search")','text=Search']:
            try:
                l=page.locator(sel).first
                if await l.count() and await l.is_visible(): print('CLICK SEARCH CONTROL:',sel); await l.click(timeout=3000); await page.wait_for_timeout(1000); break
            except: pass
        for q in qs:
            inp=None
            for sel in [
                'input[placeholder*="Search products" i]',
                'input[placeholder*="Search for products" i]',
                'input[placeholder*="Search for" i]',
                'input[type="search"]',
                'input[aria-label*="search products" i]',
                '[data-testid*="search-input" i] input',
                '[data-testid*="search" i] input'
            ]:
                try:
                    l=page.locator(sel).first
                    if await l.count() and await l.is_visible(): inp=l; break
                except: pass
            if not inp:
                print('FATAL: No product-search input found after search activation for',q)
                print('Refusing to type into a location/address input.')
                break
            try:
                await inp.fill(q); await inp.press('Enter'); print('SUBMITTED',q); await page.wait_for_timeout(7000)
            except Exception as e: print('SEARCH ACTION ERROR',e)
        dom2=await page.evaluate('''() => ({url:location.href,inputs:[...document.querySelectorAll('input,textarea')].map(x=>({placeholder:x.placeholder,aria:x.getAttribute('aria-label'),type:x.type,value:x.value})),text:document.body.innerText.slice(0,12000),resources:performance.getEntriesByType('resource').map(e=>e.name).filter(u=>u.includes('swiggy.com'))})''')
        (out/'dom_after_search.json').write_text(json.dumps(dom2,indent=2,ensure_ascii=False),encoding='utf8'); report['final_url']=page.url
        report['elapsed_ms']=round(ms()-t,1)
        report['VALID_LOCATION_FLOW_TEST'] = bool(report.get('VALID_LOCATION_FLOW_TEST', False))
        clues=[]
        for b in report['search_bundles']:
            txt=(out/b['file']).read_text(encoding='utf8',errors='ignore')
            for pat in [r'/api/instamart/[^"\']+',r'pageNumber.{0,500}',r'searchResultsOffset.{0,500}',r'INSTAMART_[A-Z_]+_PAGE']:
                for m in re.finditer(pat,txt,re.I):
                    clues.append(txt[max(0,m.start()-500):min(len(txt),m.end()+700)])
                    if len(clues)>=40: break
                if len(clues)>=40: break
        (out/'bundle_clues.txt').write_text('\n\n--- CLUE ---\n\n'.join(dict.fromkeys(clues)),encoding='utf8'); (out/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str),encoding='utf8')
        print('\nDONE'); print('Output:',out.resolve()); print('Final URL:',page.url); print('Instamart API responses:',sum('api/instamart' in x['url'] for x in report['responses'])); print('Bundles:',report['search_bundles']); await browser.close()
if __name__=='__main__': asyncio.run(main())
