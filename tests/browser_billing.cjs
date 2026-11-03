const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const net = require('node:net');
const { chromium } = require(process.env.PLAYWRIGHT_CORE_PATH || 'playwright');

async function main() {
    const root = path.resolve(__dirname, '..');
    const output = path.join(root, '.cache', 'billing-ui');
    fs.mkdirSync(output, { recursive: true });
    const fixtureDir = fs.mkdtempSync(path.join(output, 'run-'));
    const probe = net.createServer();
    probe.listen(0, '127.0.0.1');
    await once(probe, 'listening');
    const port = probe.address().port;
    await new Promise(resolve => probe.close(resolve));
    const base = `http://127.0.0.1:${port}`;
    const server = spawn(path.join(root, 'venv', 'Scripts', 'python.exe'), [
        '-B', path.join(__dirname, 'serve_billing.py'), '--database', path.join(fixtureDir, 'test.db'), '--port', String(port)
    ], { cwd: root, windowsHide: true });
    let serverOutput = '';
    server.stdout.on('data', chunk => serverOutput += chunk);
    server.stderr.on('data', chunk => serverOutput += chunk);
    let browser;
    const errors = [];
    try {
        let ready = false;
        for (let attempt = 0; attempt < 100; attempt++) {
            if (server.exitCode !== null) throw new Error(serverOutput);
            try { ready = (await fetch(`${base}/bills`)).ok; } catch {}
            if (ready) break;
            await new Promise(resolve => setTimeout(resolve, 200));
        }
        assert.ok(ready, serverOutput);
        browser = await chromium.launch({ channel: 'msedge', headless: true });
        const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
        const page = await context.newPage();
        page.on('pageerror', error => errors.push(String(error)));
        const testPassword = 'BrowserTest928!';
        async function signIn(targetPage, username) {
            await targetPage.goto(`${base}/login`, { waitUntil: 'networkidle' });
            await targetPage.locator('#username').fill(username);
            await targetPage.locator('#password').fill(testPassword);
            await targetPage.getByRole('button', { name: '登录', exact: true }).click();
            await targetPage.waitForURL(url => !url.pathname.startsWith('/login'));
        }
        async function switchUser(username) {
            await page.getByRole('button', { name: '退出登录', exact: true }).click();
            await page.waitForURL(`${base}/login`);
            await signIn(page, username);
        }
        await page.goto(`${base}/`, { waitUntil: 'networkidle' });
        await page.waitForURL(`${base}/setup`);
        await page.locator('#display-name').fill('演示管理员');
        await page.locator('#username').fill('admin');
        await page.locator('#new-password').fill(testPassword);
        await page.locator('#confirm-password').fill(testPassword);
        await page.screenshot({ path: path.join(output, 'desktop-setup.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-setup.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole('button', { name: '创建管理员', exact: true }).click();
        await page.waitForURL(`${base}/login`);
        await signIn(page, 'admin');
        for (const [username, name, role] of [['cashier', '演示收费员', 'cashier'], ['frontdesk', '演示挂号员', 'frontdesk'], ['doctor', '演示医生', 'doctor']]) {
            await page.goto(`${base}/users/create`, { waitUntil: 'networkidle' });
            await page.locator('#display-name').fill(name);
            await page.locator('#username').fill(username);
            await page.locator('#new-password').fill(testPassword);
            await page.locator('#confirm-password').fill(testPassword);
            await page.locator('#user-role').selectOption(role);
            if (role === 'doctor') await page.locator('#assigned-doctor').selectOption('1');
            await page.getByRole('button', { name: '创建账号', exact: true }).click();
            await page.waitForURL(`${base}/users`);
        }
        await page.screenshot({ path: path.join(output, 'desktop-users.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-users.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.goto(`${base}/medicines/create`, { waitUntil: 'networkidle' });
        for (const [key, value] of Object.entries({ code: 'DEMO-A', name: '测试药品', specification: '演示规格 A', unit: '盒', price: '0.10' })) {
            await page.locator(`#${key}`).fill(value);
        }
        await page.getByRole('button', { name: '保存药品', exact: true }).click();
        await page.waitForURL(`${base}/medicines`);
        assert.equal(await page.locator('tr[data-medicine-id]').count(), 1);
        await page.screenshot({ path: path.join(output, 'desktop-medicines.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-medicines.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await switchUser('cashier');
        assert.equal((await context.request.get(`${base}/users`)).status(), 403);
        assert.equal((await context.request.get(`${base}/medical-records`)).status(), 403);
        assert.equal(await page.locator('.side-nav a[href="/users"]').count(), 0);
        await page.goto(`${base}/bills/1`, { waitUntil: 'networkidle' });
        assert.ok(await page.locator('#payment-form').isVisible());
        const token = await page.locator('#payment-form [name="payment_token"]').inputValue();
        await page.locator('#received-amount').fill('10.98');
        assert.equal(await page.locator('#received-amount').evaluate(input => input.checkValidity()), false);
        await page.locator('#received-amount').fill('20.00');
        assert.equal(await page.locator('#change-amount').textContent(), '¥9.01');
        await page.locator('#payment-method').selectOption('wechat');
        assert.equal(await page.locator('#received-amount').inputValue(), '10.99');
        assert.equal(await page.locator('#received-amount').evaluate(input => input.readOnly), true);
        await page.locator('#payment-method').selectOption('cash');
        await page.locator('#received-amount').fill('20.00');
        await page.screenshot({ path: path.join(output, 'desktop-cash.png'), fullPage: true });
        for (const width of [390, 1024]) {
            await page.setViewportSize({ width, height: 844 });
            assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
            const fields = await page.locator('.payment-fields').boundingBox();
            assert.ok(fields.width > 0);
            await page.screenshot({ path: path.join(output, `cash-${width}.png`), fullPage: true });
        }
        await page.locator('#payment-confirmed').check();
        const [response] = await Promise.all([
            page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/bills/1/pay')),
            page.locator('#confirm-payment').click()
        ]);
        assert.equal(response.status(), 302);
        await page.locator('#receipt-number').waitFor();
        const receipt = await page.locator('#receipt-number').textContent();
        assert.match(receipt, /^RC\d{8}[A-F0-9]{24}$/);
        assert.equal(await page.locator('#payment-form').count(), 0);
        assert.ok((await page.locator('.payment-record').textContent()).includes('9.01'));
        assert.ok((await page.locator('#payment-operator').textContent()).includes('cashier'));
        const repeat = await context.request.post(`${base}/bills/1/pay`, { maxRedirects: 0, form: {
            payment_token: token, expected_amount: '10.99', received_amount: '20.00',
            payment_method: 'cash', confirmed: 'yes'
        }});
        assert.equal(repeat.status(), 302);
        await page.reload({ waitUntil: 'networkidle' });
        assert.equal(await page.locator('#receipt-number').textContent(), receipt);
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-paid.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.emulateMedia({ media: 'print' });
        assert.ok(await page.locator('.bill-patient').isVisible());
        assert.ok(await page.locator('#receipt-number').isVisible());
        await page.screenshot({ path: path.join(output, 'print-receipt.png'), fullPage: true });
        await page.emulateMedia({ media: 'screen' });
        await page.goto(`${base}/bills/2`, { waitUntil: 'networkidle' });
        await page.locator('#payment-method').selectOption('wechat');
        await page.locator('#payment-confirmed').check();
        await page.locator('#confirm-payment').click();
        await page.locator('#receipt-number').waitFor();
        assert.ok((await page.locator('.settlement-box').textContent()).includes('微信'));
        await switchUser('doctor');
        assert.equal((await context.request.get(`${base}/bills`)).status(), 403);
        assert.equal((await context.request.get(`${base}/medicines/create`)).status(), 403);
        await page.goto(`${base}/prescriptions/create`, { waitUntil: 'networkidle' });
        await page.locator('[name="medicine_id[]"]').selectOption('1');
        await page.locator('[name="quantity[]"]').fill('3');
        assert.equal(await page.locator('#prescription-total').textContent(), '¥0.30');
        assert.equal(await page.locator('[name="unit_price[]"]').count(), 0);
        await page.locator('#add-drug').click();
        await page.locator('[name="medicine_id[]"]').nth(1).selectOption('1');
        assert.equal(await page.locator('[name="medicine_id[]"]').first().evaluate(input => input.checkValidity()), false);
        await page.locator('.remove-drug').nth(1).click();
        assert.equal(await page.locator('[name="medicine_id[]"]').evaluate(input => input.checkValidity()), true);
        await page.screenshot({ path: path.join(output, 'desktop-prescription.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        assert.ok(await page.locator('[name="quantity[]"]').evaluate(input => {
            const bounds = input.getBoundingClientRect();
            return bounds.x >= 0 && bounds.right <= innerWidth;
        }));
        assert.ok(await page.locator('.drug-price').evaluate(element => {
            const bounds = element.getBoundingClientRect();
            return bounds.x >= 0 && bounds.right <= innerWidth;
        }));
        await page.screenshot({ path: path.join(output, 'mobile-prescription.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole('button', { name: '提交处方' }).click();
        await page.waitForURL(`${base}/prescriptions`);
        await page.getByRole('link', { name: 'RX00001', exact: true }).click();
        assert.ok((await page.locator('.data-table').textContent()).includes('演示规格 A'));
        await page.screenshot({ path: path.join(output, 'desktop-prescription-detail.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-prescription-detail.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await switchUser('admin');
        await page.goto(`${base}/medicines/1/edit`, { waitUntil: 'networkidle' });
        await page.locator('#name').fill('改名后的测试药品');
        await page.locator('#specification').fill('新规格 B');
        await page.locator('#price').fill('8.88');
        await page.getByRole('button', { name: '保存药品', exact: true }).click();
        await page.waitForURL(url => url.pathname === '/medicines');
        await page.getByRole('button', { name: '停用药品', exact: true }).click();
        await page.waitForURL(url => url.pathname === '/medicines');
        await page.goto(`${base}/prescriptions/1`, { waitUntil: 'networkidle' });
        assert.ok((await page.locator('.data-table').textContent()).includes('演示规格 A'));
        assert.ok(!(await page.locator('.data-table').textContent()).includes('新规格 B'));
        await page.goto(`${base}/prescriptions/create`, { waitUntil: 'networkidle' });
        assert.equal(await page.locator('[name="medicine_id[]"] option[data-revision]').count(), 0);
        assert.equal(await page.getByRole('button', { name: '提交处方' }).isDisabled(), true);
        await switchUser('cashier');
        await page.goto(`${base}/bills/3`, { waitUntil: 'networkidle' });
        assert.equal(await page.locator('[name="expected_amount"]').inputValue(), '0.30');
        assert.ok((await page.locator('.receipt-panel').textContent()).includes('演示规格 A'));
        await page.goto(`${base}/bills`, { waitUntil: 'networkidle' });
        assert.equal(await page.locator('form[action$="/pay"]').count(), 0);
        await page.screenshot({ path: path.join(output, 'desktop-list.png'), fullPage: true });

        await page.goto(`${base}/bills/1`, { waitUntil: 'networkidle' });
        await page.locator('.refund-section summary').click();
        assert.equal(await page.locator('#refund-reason').evaluate(input => input.checkValidity()), false);
        const reason = '患者取消就诊，已核对原收款流水并退还全部费用。';
        await page.locator('#refund-reason').fill(reason);
        await page.locator('#refund-confirmed').check();
        const refundToken = await page.locator('#refund-form [name="payment_token"]').inputValue();
        await page.screenshot({ path: path.join(output, 'desktop-refund-form.png'), fullPage: true });
        const [refundResponse] = await Promise.all([
            page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/bills/1/refund')),
            page.locator('#confirm-refund').click()
        ]);
        assert.equal(refundResponse.status(), 302);
        await page.locator('#refund-number').waitFor();
        const refundNumber = await page.locator('#refund-number').textContent();
        assert.match(refundNumber, /^RF\d{8}[A-F0-9]{24}$/);
        assert.equal(await page.locator('#receipt-number').textContent(), receipt);
        assert.equal(await page.locator('#refund-reason-text').textContent(), reason);
        assert.ok((await page.locator('#refund-operator').textContent()).includes('cashier'));
        assert.equal(await page.locator('#refund-form').count(), 0);
        assert.equal(await page.locator('#payment-form').count(), 0);
        const refundRepeat = await context.request.post(`${base}/bills/1/refund`, { maxRedirects: 0, form: {
            payment_token: refundToken, expected_amount: '10.99', payment_receipt: receipt,
            reason, confirmed: 'yes'
        }});
        assert.equal(refundRepeat.status(), 302);
        await page.reload({ waitUntil: 'networkidle' });
        assert.equal(await page.locator('#refund-number').textContent(), refundNumber);
        await page.screenshot({ path: path.join(output, 'desktop-refunded.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-refunded.png'), fullPage: true });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.emulateMedia({ media: 'print' });
        assert.ok(await page.locator('.bill-patient').isVisible());
        assert.ok(await page.locator('#receipt-number').isVisible());
        assert.ok(await page.locator('#refund-number').isVisible());
        await page.screenshot({ path: path.join(output, 'print-refund.png'), fullPage: true });
        await page.emulateMedia({ media: 'screen' });
        await page.goto(`${base}/bills?status=refunded`, { waitUntil: 'networkidle' });
        assert.equal(await page.locator('.data-table tbody tr').count(), 1);
        assert.ok((await page.locator('.data-table tbody').textContent()).includes('已退费'));
        await page.getByRole('link', { name: '收支流水', exact: true }).click();
        await page.waitForURL(`${base}/bills/transactions`);
        assert.equal(await page.locator('#income-total').textContent(), '¥21.98');
        assert.equal(await page.locator('#refund-total').textContent(), '¥10.99');
        assert.equal(await page.locator('#net-total').textContent(), '¥10.99');
        assert.equal(await page.locator('tr[data-kind]').count(), 3);
        await page.screenshot({ path: path.join(output, 'desktop-transactions.png'), fullPage: true });
        const today = (await page.locator('.date-pill').textContent()).trim();
        await page.locator('#start-date').fill(today);
        await page.locator('#end-date').fill(today);
        await page.locator('#transaction-kind').selectOption('refund');
        await page.getByRole('button', { name: '查询', exact: true }).click();
        await page.waitForURL(url => url.searchParams.get('kind') === 'refund');
        assert.equal(await page.locator('tr[data-kind="refund"]').count(), 1);
        assert.equal(await page.locator('#income-total').textContent(), '¥0.00');
        assert.equal(await page.locator('#net-total').textContent(), '¥-10.99');
        await page.locator('#transaction-kind').selectOption('');
        await page.locator('#transaction-query').fill(receipt);
        await page.getByRole('button', { name: '查询', exact: true }).click();
        await page.waitForURL(url => url.searchParams.get('q') === receipt);
        assert.equal(await page.locator('tr[data-kind]').count(), 2);
        assert.equal(await page.locator('#net-total').textContent(), '¥0.00');
        await page.setViewportSize({ width: 390, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({ path: path.join(output, 'mobile-transactions.png'), fullPage: true });

        const adminContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
        const adminPage = await adminContext.newPage();
        adminPage.on('pageerror', error => errors.push(String(error)));
        await signIn(adminPage, 'admin');
        await adminPage.goto(`${base}/operation-logs?action=bill.refund`, { waitUntil: 'networkidle' });
        assert.equal(await adminPage.locator('.audit-table tbody tr').count(), 1);
        assert.ok((await adminPage.locator('.audit-table tbody').textContent()).includes('cashier'));
        await adminPage.screenshot({ path: path.join(output, 'desktop-audit.png'), fullPage: true });

        const nextDay = new Date(new Date(`${today}T12:00:00Z`).getTime() + 86400000).toISOString().slice(0, 10);
        await adminPage.goto(`${base}/schedules/create`, { waitUntil: 'networkidle' });
        await adminPage.locator('#doctor-id').selectOption('1');
        await adminPage.locator('#visit-date').fill(nextDay);
        await adminPage.locator('#period').selectOption('pm');
        await adminPage.locator('#capacity').fill('1');
        await adminPage.locator('#schedule-fee').fill('15.50');
        await adminPage.getByRole('button', { name: '保存排班', exact: true }).click();
        await adminPage.waitForURL(url => url.pathname === '/schedules');
        assert.equal(await adminPage.locator('tr[data-schedule]').count(), 1);
        await adminPage.screenshot({ path: path.join(output, 'desktop-schedules.png'), fullPage: true });
        await adminPage.setViewportSize({ width: 390, height: 844 });
        assert.ok(await adminPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await adminPage.screenshot({ path: path.join(output, 'mobile-schedules.png'), fullPage: true });
        await adminPage.setViewportSize({ width: 1440, height: 1000 });
        await switchUser('frontdesk');
        async function bookFuture() {
            await page.goto(`${base}/registrations/create?visit_date=${nextDay}`, { waitUntil: 'networkidle' });
            await page.locator('#appointment-patient').selectOption('1');
            await page.locator('#appointment-slot').selectOption('1');
            assert.equal(await page.locator('#appointment-fee').textContent(), '¥15.50');
            await page.setViewportSize({ width: 1440, height: 1000 });
            await page.screenshot({ path: path.join(output, 'desktop-appointment.png'), fullPage: true });
            await page.setViewportSize({ width: 390, height: 844 });
            assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
            await page.screenshot({ path: path.join(output, 'mobile-appointment.png'), fullPage: true });
            await page.locator('#confirm-appointment').click();
            await page.waitForURL(url => url.pathname === '/registrations');
            return Number(await page.locator('tr[data-registration]').first().getAttribute('data-registration'));
        }
        async function cancelFuture(rid) {
            await page.goto(`${base}/registrations?date=${nextDay}`, { waitUntil: 'networkidle' });
            const [response] = await Promise.all([
                page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith(`/registrations/${rid}/status`)),
                page.locator(`tr[data-registration="${rid}"]`).getByRole('button', { name: '取消', exact: true }).click()
            ]);
            return response.status();
        }
        const firstAppointment = await bookFuture();
        assert.ok((await page.locator(`tr[data-registration="${firstAppointment}"]`).textContent()).includes('001号'));
        await page.goto(`${base}/schedules?date=${nextDay}`, { waitUntil: 'networkidle' });
        assert.ok((await page.locator('tr[data-schedule="1"]').textContent()).includes('已满'));
        assert.equal(await cancelFuture(firstAppointment), 302);
        const secondAppointment = await bookFuture();
        assert.ok((await page.locator(`tr[data-registration="${secondAppointment}"]`).textContent()).includes('002号'));
        await switchUser('cashier');
        await page.goto(`${base}/bills?status=void`, { waitUntil: 'networkidle' });
        assert.equal(await page.locator('.data-table tbody tr').count(), 1);
        assert.ok((await page.locator('.data-table tbody').textContent()).includes('已作废'));
        await page.goto(`${base}/bills?status=unpaid`, { waitUntil: 'networkidle' });
        await page.locator('.data-table tbody tr').first().getByRole('link', { name: '核对收款' }).click();
        await page.locator('#received-amount').fill('20.00');
        await page.locator('#payment-confirmed').check();
        await page.locator('#confirm-payment').click();
        await page.locator('#receipt-number').waitFor();
        const appointmentBill = page.url();
        await switchUser('frontdesk');
        assert.equal(await cancelFuture(secondAppointment), 409);
        await switchUser('cashier');
        await page.goto(appointmentBill, { waitUntil: 'networkidle' });
        await page.locator('.refund-section summary').click();
        await page.locator('#refund-reason').fill('取消预约，全额退费');
        await page.locator('#refund-confirmed').check();
        await page.locator('#confirm-refund').click();
        await page.locator('#refund-number').waitFor();
        await switchUser('frontdesk');
        assert.equal(await cancelFuture(secondAppointment), 302);
        await page.goto(`${base}/schedules?date=${nextDay}`, { waitUntil: 'networkidle' });
        assert.ok((await page.locator('tr[data-schedule="1"]').textContent()).includes('0 / 1'));
        await switchUser('cashier');

        await adminPage.goto(`${base}/users`, { waitUntil: 'networkidle' });
        await adminPage.locator('tr[data-user="cashier"]').getByRole('button', { name: '停用', exact: true }).click();
        await adminPage.waitForURL(`${base}/users`);
        await page.goto(`${base}/bills`, { waitUntil: 'networkidle' });
        await page.waitForURL(url => url.pathname === '/login');
        await page.screenshot({ path: path.join(output, 'mobile-login.png'), fullPage: true });
        await adminContext.close();
        assert.deepEqual(errors, []);
        console.log('Browser checks passed: medicine catalog, selection, duplicates, snapshots after price/spec changes and disable, prescription details, roles, schedules, appointment, payment/refund, audit, responsive and print.');
        console.log(`Screenshots: ${output}`);
        console.log(`Isolated fixture database: ${fixtureDir}`);
    } finally {
        if (browser) await browser.close();
        if (server.exitCode === null) {
            const exited = once(server, 'exit');
            server.kill();
            await exited;
        }
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
