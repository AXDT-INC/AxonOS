/* CARD behavior uses production JavaScript with a mock DOM and provider.
 * No Stripe credentials, external calls, local credit grants, or real payments. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const wallet = '0x' + '1'.repeat(40);
const paymentId = 'c5c91363-1c1f-44f1-852e-182379be0835';
const flush = () => new Promise(resolve => setImmediate(resolve));
const noop = () => {};
function between(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, start);
    return source.slice(from, to);
}
function node() {
    const classes = new Set();
    return { value: '50.00', textContent: '', hidden: false, style: {}, dataset: {},
        classList: { add: x => classes.add(x), remove: x => classes.delete(x),
            toggle: (x, force) => force ? classes.add(x) : classes.delete(x) },
        setAttribute: noop, getAttribute: () => '', addEventListener: noop,
        dispatchEvent: noop, closest() { return this; }, querySelectorAll: () => [] };
}
function browser(enabled = true) {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, node());
        return elements.get(id);
    };
    const state = { requests: [], redirects: [], timers: [], busy: [], saved: new Map(),
        refreshes: 0, quotes: [], response: { checkout_url: 'https://checkout.stripe.com/c/pay/test', payment_id: paymentId },
        ok: true, balance: null, restored: [], operation: 0 };
    const context = vm.createContext({
        window: { verifiedWalletAddress: wallet, verifiedWalletAuthToken: 'verified-identity-token',
            location: { href: 'https://axon.example/vnc.html', origin: 'https://axon.example',
                assign: url => state.redirects.push(url) },
            sessionStorage: { setItem: (k, v) => state.saved.set(k, v),
                getItem: k => state.saved.get(k), removeItem: k => state.saved.delete(k) },
            history: { replaceState: (_a, _b, url) => { context.window.location.href = url; } } },
        document: { getElementById: element, querySelector: element, querySelectorAll: () => [] },
        axonosConfig: { card_payments_enabled: enabled, card_min_amount_usd: 1,
            card_max_amount_usd: 10000, card_credits_per_usd: 60,
            axgt_bonus_percent: 25, credit_per_100_axgt: 60, eth_deposits_enabled: true,
            usdc_deposits_enabled: true, usdc_contract_address: '0x' + '3'.repeat(40),
            axgt_direct_deposits_enabled: true },
        axonosActivePayRail: 'card', axonosSelectedPaymentToken: 'card',
        axonosSelectedGpuProfile: 'small', axonosCurrentWizardStep: 3,
        axonosGuestActive: () => false,
        axonosBeginPaymentOperation: () => ({ generation: ++state.operation }),
        axonosPaymentOperationIsCurrent: op => op.generation === state.operation && context.window.verifiedWalletAddress === wallet,
        axonosPaymentIdentityIsCurrent: w => context.window.verifiedWalletAddress === w,
        axonosPromiseWithTimeout: p => p,
        setPayDepositButtonsBusy: busy => state.busy.push(busy),
        axonosProfileLabel: () => 'Single', axonosGpuCountForProfile: () => 1,
        axonosWizardScheduleQuote: (token, amount) => state.quotes.push({ token, amount }),
        axonosSyncTopbar: (_wallet, balance) => { state.balance = balance; },
        axonosFetchWalletAccessStatus: async () => {
            state.refreshes++;
            return { ok: true, data: { remaining_minutes: 2999 } };
        },
        fetch: async (url, options) => {
            state.requests.push({ url, options });
            if (state.fetch) return state.fetch(url, options);
            return { ok: state.ok, json: async () => state.response };
        },
        axonosUpdateWalletNetworkLineForRail: noop, updateTokenPayHint: noop,
        localStorage: { setItem: noop },
        setTimeout: callback => state.timers.push(callback), clearTimeout: noop,
        URL, Event: class {}, console,
    });
    vm.runInContext(between('var axonosCardCheckoutBusy =', 'var cardInput ='), context);
    vm.runInContext(between('function axonosPayRailAvailability(', 'var payEthInput ='), context);
    vm.runInContext(between('function axonosUpdatePayCalculator(', '/** Debounced live quote'), context);
    context.axonosRestoreCardWizard = (...args) => state.restored.push(args);
    return { context, state, element };
}
async function main() {
    // Both selectors name the rail CARD and remain hidden before config loads.
    assert.match(source, /id="axonos_wizard_card_tab"[^>]+data-token="card"[^>]+display:none;[^>]*>CARD/);
    assert.match(source, /id="axonos_pay_rail_card"[^>]+data-rail="card"[^>]+display:none;[^>]*>CARD/);
    assert.doesNotMatch(source, /STRIPE_SECRET_KEY|STRIPE_WEBHOOK_SECRET/);
    {
        const { context: c, state: s, element } = browser(false);
        c.axonosSelectPayRail('card');
        assert.equal(c.axonosActivePayRail, 'usdc');
        assert.equal(element('axonos_pay_rail_card').style.display, 'none');
        await c.axonosStartCardCheckout('50.00', false);
        assert.equal(s.requests.length, 0);
    }
    {
        const { context: c, element } = browser();
        for (const rail of ['eth', 'usdc', 'axgt', 'card']) {
            c.axonosSelectPayRail(rail);
            assert.equal(c.axonosActivePayRail, rail);
            assert.equal(element('axonos_pay_rail_' + rail).style.opacity, '1');
        }
        assert.equal(element('axonos_crypto_deposit_claim').style.display, 'none');
        assert.equal(element('axonos_card_payment_panel').style.display, '');
        assert.equal(element('axonos_discount_panel').style.display, 'none');
    }
    {
        const { context: c, state: s, element } = browser();
        c.axonosUpdatePayCalculator();
        assert.equal(element('axonos_wizard_pay_credits').textContent, '≈ 3000 credits');
        assert.equal(element('axonos_wizard_pay_ticker').textContent, 'USD');
        assert.equal(element('axonos_wizard_pay_bonus_row').style.display, 'none');
        assert.equal(s.quotes.length, 0);
        for (const rail of ['usdc', 'eth']) {
            c.axonosSelectedPaymentToken = rail;
            c.axonosUpdatePayCalculator();
            assert.equal(s.quotes.at(-1).token, rail);
        }
        c.axonosSelectedPaymentToken = 'axgt';
        element('axonos_wizard_pay_amount').value = '100';
        c.axonosUpdatePayCalculator();
        assert.equal(element('axonos_wizard_pay_credits').textContent, '75 credits');
        assert.equal(element('axonos_wizard_pay_bonus_row').style.display, 'flex');
        c.axonosConfig.dynamic_pricing_enabled = true;
        c.axonosUpdatePayCalculator();
        assert.equal(s.quotes.at(-1).token, 'axgt');
    }
    for (const rail of ['usdc', 'eth', 'axgt']) {
        const { context: c, state: s, element } = browser();
        vm.runInContext(between('var axonosWizardQuoteTimer =', '/** Launch a demo session'), c);
        let resolve;
        s.fetch = () => new Promise(r => { resolve = r; });
        c.axonosSelectedPaymentToken = rail;
        c.axonosWizardScheduleQuote(rail, 50);
        s.timers.shift()();
        c.axonosSelectedPaymentToken = 'card';
        c.axonosUpdatePayCalculator();
        resolve({ json: async () => ({ ok: true, estimated_minutes: 99999, discount_percent: 50 }) });
        await flush();
        assert.equal(element('axonos_wizard_pay_credits').textContent, '≈ 3000 credits');
        assert.equal(element('axonos_wizard_pay_bonus_row').style.display, 'none');
    }
    {
        const { context: c, state: s } = browser();
        for (const invalid of ['', '-1', '0', '0.99', '10001', '1.001', '1e2', 'Infinity', '5junk']) {
            await c.axonosStartCardCheckout(invalid, false);
        }
        assert.equal(s.requests.length, 0);
        c.window.verifiedWalletAuthToken = null;
        await c.axonosStartCardCheckout('50', false);
        c.window.verifiedWalletAuthToken = 'verified-identity-token';
        c.window.verifiedWalletAddress = null;
        await c.axonosStartCardCheckout('50', false);
        assert.equal(s.requests.length, 0);
    }
    {
        const { context: c, state: s } = browser();
        await c.axonosStartCardCheckout('50.00', false);
        assert.equal(s.requests.length, 1);
        const request = s.requests[0];
        assert.equal(request.url, '/api/payments/stripe/checkout');
        assert.deepEqual(JSON.parse(request.options.body), { amount_usd: '50.00' });
        assert.equal(request.options.headers['X-AXGT-Auth-Token'], 'verified-identity-token');
        assert.equal(request.options.credentials, 'include');
        assert.equal(s.redirects[0], s.response.checkout_url);
        assert.deepEqual(s.busy, [true, false]);
        assert.equal(JSON.parse(s.saved.get('axonos_card_checkout')).wallet, wallet);
    }
    for (const checkout_url of ['https://evil.test/pay', 'http://checkout.stripe.com/pay', 'https://checkout.stripe.com.evil.test/pay']) {
        const { context: c, state: s } = browser();
        s.response.checkout_url = checkout_url;
        await c.axonosStartCardCheckout('50', false);
        assert.equal(s.redirects.length, 0);
    }
    {
        const { context: c, state: s } = browser();
        let resolve;
        s.fetch = () => new Promise(r => { resolve = r; });
        const pending = c.axonosStartCardCheckout('50', false);
        await c.axonosStartCardCheckout('50', false);
        assert.equal(s.requests.length, 1);
        c.window.verifiedWalletAddress = '0x' + '2'.repeat(40);
        resolve({ ok: true, json: async () => s.response });
        await pending;
        assert.equal(s.redirects.length, 0);
    }
    {
        const { context: c, state: s, element } = browser();
        c.window.location.href += '?card_payment=success&payment_id=' + paymentId;
        s.response = { credited: false, status: 'pending', credits_added: 3000 };
        c.axonosHandleCardReturn();
        c.axonosHandleCardReturn();
        await flush();
        assert.equal(s.requests.length, 1);
        assert.equal(s.refreshes, 0); // A success URL and purchased amount are never balance authority.
        assert.equal(c.axonosCardWizardFunded, false);
        s.response = { credited: true, status: 'succeeded', credits_added: 3000 };
        s.timers.shift()();
        await flush();
        assert.equal(s.refreshes, 1);
        assert.equal(s.balance, 2999); // Existing authoritative balance, not 3000.
        assert.equal(c.axonosCardWizardFunded, true);
        assert.equal(element('axonos_wizard_rail_balance').textContent, '2999 cr');
        assert.equal(new URL(c.window.location.href).searchParams.has('payment_id'), false);
        assert.ok(s.requests.every(x => x.url.startsWith('/api/payments/stripe/status?') && !x.options.body));
    }
    {
        const { context: c, state: s, element } = browser();
        c.window.location.href += '?card_payment=success&payment_id=' + paymentId;
        s.response = { credited: false, status: 'refunded', reconciliation_required: true };
        c.axonosHandleCardReturn();
        await flush();
        assert.equal(s.refreshes, 0);
        assert.equal(s.timers.length, 0);
        assert.match(element('axonos_card_return_message').textContent, /operator review/);
    }
    for (const status of ['failed', 'expired', 'cancelled']) {
        const { context: c, state: s } = browser();
        c.window.location.href += '?card_payment=success&payment_id=' + paymentId;
        s.response = { credited: false, status };
        c.axonosHandleCardReturn();
        await flush();
        assert.equal(s.refreshes, 0);
        assert.equal(s.timers.length, 0);
        assert.equal(c.axonosCardWizardFunded, false);
    }
    {
        const { context: c, state: s } = browser();
        c.window.location.href += '?card_payment=success&payment_id=' + paymentId;
        c.window.verifiedWalletAuthToken = null;
        c.axonosHandleCardReturn();
        assert.equal(s.requests.length, 0);
    }
    console.log('card payment runtime checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
