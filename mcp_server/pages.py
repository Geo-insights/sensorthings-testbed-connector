"""HTML pages for auth and pricing, served directly from the MCP server.

- ``GET /auth`` — Supabase Auth UI (signup/login) + JWT display
- ``GET /pricing`` — Pricing page with Stripe Checkout button
- ``GET /payment/success`` — Post-checkout success page

All pages are self-contained HTML with inline CSS/JS using the Supabase JS
client library from CDN. No build step needed.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

from mcp_server.config import settings

_COMMON_STYLES = """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            background: #0a0f1c; color: #e2e8f0;
            min-height: 100vh;
        }
        .container { max-width: 640px; margin: 0 auto; padding: 2rem 1.5rem; }
        h1 { font-size: 1.75rem; font-weight: 700; margin-bottom: 0.5rem; }
        h2 { font-size: 1.25rem; font-weight: 600; margin-bottom: 0.75rem; color: #94a3b8; }
        p { color: #94a3b8; line-height: 1.6; margin-bottom: 1rem; }
        a { color: #60a5fa; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .card {
            background: #1e293b; border: 1px solid #334155;
            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
        }
        .btn {
            display: inline-block; padding: 0.75rem 1.5rem;
            background: #3b82f6; color: white; border: none;
            border-radius: 8px; font-size: 1rem; font-weight: 600;
            cursor: pointer; transition: background 0.2s;
        }
        .btn:hover { background: #2563eb; }
        .btn:disabled { background: #475569; cursor: not-allowed; }
        .btn-secondary { background: #334155; }
        .btn-secondary:hover { background: #475569; }
        .token-box {
            background: #0f172a; border: 1px solid #334155;
            border-radius: 8px; padding: 1rem; font-family: monospace;
            font-size: 0.75rem; word-break: break-all; margin: 1rem 0;
            max-height: 120px; overflow-y: auto;
        }
        .badge {
            display: inline-block; padding: 0.25rem 0.75rem;
            border-radius: 9999px; font-size: 0.75rem; font-weight: 600;
        }
        .badge-free { background: #1e3a5f; color: #60a5fa; }
        .badge-premium { background: #164e3a; color: #34d399; }
        .hidden { display: none; }
        .logo { font-size: 1.5rem; font-weight: 800; color: #60a5fa; margin-bottom: 2rem; }
        .nav { display: flex; gap: 1.5rem; margin-bottom: 2rem; }
        #status-msg { padding: 0.75rem; border-radius: 8px; margin-bottom: 1rem; }
        .error { background: #3b1219; color: #fca5a5; }
        .success { background: #14271a; color: #86efac; }
        .form-group { margin-bottom: 1rem; }
        .form-group label { display: block; margin-bottom: 0.25rem; color: #94a3b8; font-size: 0.875rem; }
        .form-group input {
            width: 100%; padding: 0.625rem; background: #0f172a;
            border: 1px solid #334155; border-radius: 8px; color: #e2e8f0;
            font-size: 1rem;
        }
        .form-group input:focus { outline: none; border-color: #3b82f6; }
        .tabs { display: flex; gap: 0; margin-bottom: 1.5rem; }
        .tab {
            flex: 1; padding: 0.75rem; text-align: center; cursor: pointer;
            border-bottom: 2px solid #334155; color: #94a3b8; transition: all 0.2s;
        }
        .tab.active { border-color: #3b82f6; color: #e2e8f0; }
    </style>
"""


async def auth_page(request: Request) -> HTMLResponse:
    """Supabase Auth UI — signup, login, JWT display."""
    supabase_url = settings.supabase_project_url
    anon_key = settings.supabase_anon_key

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Geo-Insights MCP — Sign In</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <div class="logo">Geo-Insights MCP</div>
        <div class="nav">
            <a href="/auth" style="color:#e2e8f0;font-weight:600">Account</a>
            <a href="/pricing">Pricing</a>
        </div>

        <!-- Logged-out state -->
        <div id="auth-form">
            <div class="tabs">
                <div class="tab active" onclick="showTab('login')">Sign In</div>
                <div class="tab" onclick="showTab('signup')">Sign Up</div>
            </div>
            <div id="status-msg" class="hidden"></div>
            <div id="login-form">
                <div class="form-group">
                    <label>Email</label>
                    <input type="email" id="email" placeholder="you@example.com">
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" id="password" placeholder="••••••••">
                </div>
                <button class="btn" style="width:100%" id="auth-btn" onclick="doAuth()">Sign In</button>
            </div>
        </div>

        <!-- Logged-in state -->
        <div id="user-panel" class="hidden">
            <div class="card">
                <h2>Signed in as</h2>
                <p id="user-email" style="color:#e2e8f0;font-weight:600"></p>
                <p id="user-scopes"></p>
            </div>
            <div class="card">
                <h2>Your Access Token (JWT)</h2>
                <p>Use this token to authenticate MCP tool calls. Add it as a Bearer token in your MCP client configuration.</p>
                <div class="token-box" id="jwt-token"></div>
                <button class="btn btn-secondary" onclick="copyToken()">Copy Token</button>
                <span id="copy-confirm" class="hidden" style="color:#34d399;margin-left:0.5rem">Copied!</span>
            </div>
            <div class="card">
                <h2>MCP Client Configuration</h2>
                <p>Add this to your Claude Desktop / Cursor config:</p>
                <div class="token-box" id="mcp-config"></div>
            </div>
            <button class="btn btn-secondary" onclick="signOut()" style="margin-top:1rem">Sign Out</button>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
    <script>
        const sbClient = window.supabase.createClient('{supabase_url}', '{anon_key}');
        let currentMode = 'login';

        function showTab(mode) {{
            currentMode = mode;
            document.querySelectorAll('.tab').forEach((t, i) => {{
                t.classList.toggle('active', (mode === 'login' && i === 0) || (mode === 'signup' && i === 1));
            }});
            document.getElementById('auth-btn').textContent = mode === 'login' ? 'Sign In' : 'Create Account';
        }}

        function showMsg(text, type) {{
            const el = document.getElementById('status-msg');
            el.textContent = text;
            el.className = type;
            el.classList.remove('hidden');
        }}

        async function doAuth() {{
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            if (!email || !password) {{ showMsg('Enter email and password', 'error'); return; }}

            const btn = document.getElementById('auth-btn');
            btn.disabled = true;
            btn.textContent = 'Loading...';

            let result;
            if (currentMode === 'signup') {{
                result = await sbClient.auth.signUp({{ email, password }});
            }} else {{
                result = await sbClient.auth.signInWithPassword({{ email, password }});
            }}

            btn.disabled = false;
            btn.textContent = currentMode === 'login' ? 'Sign In' : 'Create Account';

            if (result.error) {{
                showMsg(result.error.message, 'error');
            }} else if (currentMode === 'signup' && !result.data.session) {{
                showMsg('Check your email for a confirmation link!', 'success');
            }}
        }}

        async function signOut() {{
            await sbClient.auth.signOut();
        }}

        function copyToken() {{
            const token = document.getElementById('jwt-token').textContent;
            navigator.clipboard.writeText(token);
            document.getElementById('copy-confirm').classList.remove('hidden');
            setTimeout(() => document.getElementById('copy-confirm').classList.add('hidden'), 2000);
        }}

        function updateUI(session) {{
            if (session) {{
                document.getElementById('auth-form').classList.add('hidden');
                document.getElementById('user-panel').classList.remove('hidden');
                document.getElementById('user-email').textContent = session.user.email;

                const scopes = session.user.app_metadata?.mcp_scopes || ['mcp:tools:free'];
                const badges = scopes.map(s =>
                    s.includes('premium')
                        ? '<span class="badge badge-premium">Premium</span>'
                        : '<span class="badge badge-free">Free</span>'
                ).join(' ');
                document.getElementById('user-scopes').innerHTML = badges;
                document.getElementById('jwt-token').textContent = session.access_token;

                const config = JSON.stringify({{
                    "mcpServers": {{
                        "geo-insights": {{
                            "url": "https://mcp.geo-insights.nl/mcp",
                            "headers": {{
                                "Authorization": "Bearer " + session.access_token
                            }}
                        }}
                    }}
                }}, null, 2);
                document.getElementById('mcp-config').textContent = config;
            }} else {{
                document.getElementById('auth-form').classList.remove('hidden');
                document.getElementById('user-panel').classList.add('hidden');
            }}
        }}

        // Listen for auth changes
        sbClient.auth.onAuthStateChange((event, session) => updateUI(session));

        // Check existing session
        sbClient.auth.getSession().then(({{ data }}) => updateUI(data.session));
    </script>
</body>
</html>""")


async def pricing_page(request: Request) -> HTMLResponse:
    """Pricing page with Stripe Checkout integration."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Geo-Insights MCP — Pricing</title>
    {_COMMON_STYLES}
    <style>
        .price {{ font-size: 2.5rem; font-weight: 800; color: #e2e8f0; }}
        .price-period {{ font-size: 1rem; color: #94a3b8; font-weight: 400; }}
        .features {{ list-style: none; padding: 0; margin: 1.5rem 0; }}
        .features li {{ padding: 0.5rem 0; color: #cbd5e1; }}
        .features li::before {{ content: "\\2713 "; color: #34d399; font-weight: bold; margin-right: 0.5rem; }}
        .plan-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }}
        @media (max-width: 640px) {{ .plan-grid {{ grid-template-columns: 1fr; }} }}
    </style>
</head>
<body>
    <div class="container" style="max-width:800px">
        <div class="logo">Geo-Insights MCP</div>
        <div class="nav">
            <a href="/auth">Account</a>
            <a href="/pricing" style="color:#e2e8f0;font-weight:600">Pricing</a>
        </div>

        <h1>MCP Tools for Climate Adaptation</h1>
        <p style="margin-bottom:2rem">Real-time environmental data and BGI measure intelligence for the Dutch public sector.</p>

        <div id="status-msg" class="hidden"></div>

        <div class="plan-grid">
            <div class="card">
                <h2>Free</h2>
                <div class="price">€0 <span class="price-period">/mo</span></div>
                <ul class="features">
                    <li>5 sensor data tools</li>
                    <li>35 environmental parameters</li>
                    <li>10 data sources (KNMI, RIVM, etc.)</li>
                    <li>100 requests/min</li>
                    <li>Latest readings + history</li>
                </ul>
                <a href="/auth" class="btn btn-secondary" style="width:100%;text-align:center;text-decoration:none">Sign Up Free</a>
            </div>
            <div class="card" style="border-color:#3b82f6">
                <h2 style="color:#60a5fa">Premium</h2>
                <div class="price" id="price-display">...</div>
                <ul class="features">
                    <li>Everything in Free</li>
                    <li>7 BGI catalog tools</li>
                    <li>44 climate adaptation measures</li>
                    <li>Policy requirements per municipality</li>
                    <li>Measure ranking + comparison</li>
                    <li>500 requests/min</li>
                    <li>Priority support</li>
                </ul>
                <button class="btn" style="width:100%" id="checkout-btn" onclick="startCheckout()">Subscribe</button>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
    <script>
        const sbClient = window.supabase.createClient(
            '{settings.supabase_project_url}', '{settings.supabase_anon_key}'
        );

        function showMsg(text, type) {{
            const el = document.getElementById('status-msg');
            el.textContent = text;
            el.className = type;
            el.classList.remove('hidden');
        }}

        async function startCheckout() {{
            const {{ data }} = await sbClient.auth.getSession();
            if (!data.session) {{
                window.location.href = '/auth';
                return;
            }}

            const btn = document.getElementById('checkout-btn');
            btn.disabled = true;
            btn.textContent = 'Redirecting...';

            try {{
                const resp = await fetch('/checkout', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        user_id: data.session.user.id,
                        email: data.session.user.email,
                    }}),
                }});
                const result = await resp.json();
                if (result.checkout_url) {{
                    window.location.href = result.checkout_url;
                }} else {{
                    showMsg(result.message || 'Failed to create checkout', 'error');
                    btn.disabled = false;
                    btn.textContent = 'Subscribe';
                }}
            }} catch (e) {{
                showMsg('Network error: ' + e.message, 'error');
                btn.disabled = false;
                btn.textContent = 'Subscribe';
            }}
        }}

        // Update price display (you can hardcode or fetch from Stripe)
        document.getElementById('price-display').innerHTML = '<span style="font-size:1rem;color:#94a3b8">Contact us</span>';
    </script>
</body>
</html>""")


async def payment_success_page(request: Request) -> HTMLResponse:
    """Post-checkout success page."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Geo-Insights MCP — Payment Successful</title>
    {_COMMON_STYLES}
</head>
<body>
    <div class="container">
        <div class="logo">Geo-Insights MCP</div>
        <div class="card" style="text-align:center;padding:3rem 1.5rem">
            <h1 style="color:#34d399;margin-bottom:1rem">Payment Successful!</h1>
            <p>Your premium access is being activated. This usually takes a few seconds.</p>
            <p>Sign out and back in to refresh your token with premium scopes.</p>
            <div style="margin-top:2rem;display:flex;gap:1rem;justify-content:center">
                <a href="/auth" class="btn">Go to Account</a>
                <a href="/pricing" class="btn btn-secondary">Back to Pricing</a>
            </div>
        </div>
    </div>
</body>
</html>""")
