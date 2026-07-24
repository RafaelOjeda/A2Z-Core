"""Landing page router for A2Z Core."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

LANDING_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>A2Z — messaging and invoicing, unified</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #faf9f5;
            color: #1a1a1a;
            line-height: 1.6;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 0 20px;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 24px 0;
            border-bottom: 1px solid #e5e1d9;
        }
        .logo {
            font-size: 24px;
            font-weight: 700;
            letter-spacing: -1px;
        }
        nav a {
            margin-left: 32px;
            text-decoration: none;
            color: #666;
            font-size: 14px;
            transition: color 0.2s;
        }
        nav a:hover {
            color: #1a1a1a;
        }
        .hero {
            padding: 80px 0;
            text-align: center;
        }
        h1 {
            font-size: 56px;
            font-weight: 700;
            margin-bottom: 16px;
            letter-spacing: -2px;
        }
        .subtitle {
            font-size: 18px;
            color: #666;
            margin-bottom: 48px;
            max-width: 600px;
            margin-left: auto;
            margin-right: auto;
        }
        .cta-buttons {
            display: flex;
            gap: 16px;
            justify-content: center;
            flex-wrap: wrap;
            margin-bottom: 80px;
        }
        .btn {
            padding: 12px 28px;
            border-radius: 8px;
            border: none;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s;
            display: inline-block;
        }
        .btn-primary {
            background: #1a1a1a;
            color: #fff;
        }
        .btn-primary:hover {
            background: #333;
        }
        .btn-secondary {
            background: #fff;
            color: #1a1a1a;
            border: 1px solid #e5e1d9;
        }
        .btn-secondary:hover {
            border-color: #1a1a1a;
            background: #f9f9f9;
        }
        .features {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 32px;
            padding: 80px 0;
            border-top: 1px solid #e5e1d9;
        }
        .feature {
            padding: 32px;
            background: #fff;
            border-radius: 12px;
            border: 1px solid #e5e1d9;
        }
        .feature-icon {
            font-size: 32px;
            margin-bottom: 16px;
        }
        .feature h3 {
            font-size: 18px;
            margin-bottom: 8px;
        }
        .feature p {
            color: #666;
            font-size: 14px;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 24px;
            padding: 80px 0;
            border-top: 1px solid #e5e1d9;
            border-bottom: 1px solid #e5e1d9;
        }
        .stat {
            text-align: center;
        }
        .stat-number {
            font-size: 36px;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .stat-label {
            color: #666;
            font-size: 14px;
        }
        footer {
            padding: 48px 0;
            text-align: center;
            color: #666;
            font-size: 14px;
            border-top: 1px solid #e5e1d9;
        }
        .integration-section {
            padding: 80px 0;
        }
        .section-title {
            font-size: 32px;
            font-weight: 700;
            margin-bottom: 48px;
            text-align: center;
        }
        .integration-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 24px;
        }
        .integration-card {
            padding: 32px;
            background: #fff;
            border-radius: 12px;
            border: 1px solid #e5e1d9;
            text-align: center;
        }
        .integration-card h4 {
            margin-top: 16px;
            font-size: 14px;
        }
        @media (max-width: 768px) {
            h1 {
                font-size: 36px;
            }
            .subtitle {
                font-size: 16px;
            }
            nav a {
                margin-left: 16px;
            }
            .cta-buttons {
                flex-direction: column;
                align-items: center;
            }
            .btn {
                width: 100%;
                max-width: 300px;
            }
        }
    </style>
</head>
<body>
    <div id="root"></div>
    <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script>
        const { useEffect, useState } = React;

        function LandingPage() {
            return React.createElement('div', null,
                React.createElement('header', { className: 'container' },
                    React.createElement('div', { className: 'logo' }, 'A2Z'),
                    React.createElement('nav', null,
                        React.createElement('a', { href: '#features' }, 'Features'),
                        React.createElement('a', { href: '#integrations' }, 'Integrations'),
                        React.createElement('a', { href: '/v1/health' }, 'Status')
                    )
                ),
                React.createElement('div', { className: 'container' },
                    React.createElement('section', { className: 'hero' },
                        React.createElement('h1', null, 'A2Z — messaging and invoicing, unified'),
                        React.createElement('p', { className: 'subtitle' },
                            'A unified platform for small businesses. One backbone for invoicing, omni-channel messaging, and more. All your essential tools in one place.'
                        ),
                        React.createElement('div', { className: 'cta-buttons' },
                            React.createElement('button', { className: 'btn btn-primary' }, 'Get Started'),
                            React.createElement('button', { className: 'btn btn-secondary' }, 'View Docs')
                        )
                    ),
                    React.createElement('section', { className: 'stats', id: 'stats' },
                        React.createElement('div', { className: 'stat' },
                            React.createElement('div', { className: 'stat-number' }, '3M+'),
                            React.createElement('div', { className: 'stat-label' }, 'Emails sent monthly')
                        ),
                        React.createElement('div', { className: 'stat' },
                            React.createElement('div', { className: 'stat-number' }, '99.9%'),
                            React.createElement('div', { className: 'stat-label' }, 'Uptime SLA')
                        ),
                        React.createElement('div', { className: 'stat' },
                            React.createElement('div', { className: 'stat-number' }, '7 years'),
                            React.createElement('div', { className: 'stat-label' }, 'Audit log retention')
                        ),
                        React.createElement('div', { className: 'stat' },
                            React.createElement('div', { className: 'stat-number' }, '< 50ms'),
                            React.createElement('div', { className: 'stat-label' }, 'API response time')
                        )
                    ),
                    React.createElement('section', { className: 'features', id: 'features' },
                        React.createElement('div', { className: 'feature' },
                            React.createElement('div', { className: 'feature-icon' }, '📧'),
                            React.createElement('h3', null, 'Smart Email'),
                            React.createElement('p', null, 'Send emails, track delivery, manage suppressions, and handle bounces automatically.')
                        ),
                        React.createElement('div', { className: 'feature' },
                            React.createElement('div', { className: 'feature-icon' }, '💳'),
                            React.createElement('h3', null, 'Invoicing'),
                            React.createElement('p', null, 'Create, send, and track invoices with automatic payment reminders and integrations.')
                        ),
                        React.createElement('div', { className: 'feature' },
                            React.createElement('div', { className: 'feature-icon' }, '💬'),
                            React.createElement('h3', null, 'Omni-Channel'),
                            React.createElement('p', null, 'Reach customers via email, SMS, WhatsApp, and other channels from one dashboard.')
                        ),
                        React.createElement('div', { className: 'feature' },
                            React.createElement('div', { className: 'feature-icon' }, '🔐'),
                            React.createElement('h3', null, 'Secure & Compliant'),
                            React.createElement('p', null, 'Enterprise-grade security, SOC 2 compliance, and 7-year audit logs.')
                        ),
                        React.createElement('div', { className: 'feature' },
                            React.createElement('div', { className: 'feature-icon' }, '⚡'),
                            React.createElement('h3', null, 'Lightning Fast'),
                            React.createElement('p', null, 'Sub-50ms API response times and optimized for scale with Redis caching.')
                        ),
                        React.createElement('div', { className: 'feature' },
                            React.createElement('div', { className: 'feature-icon' }, '📊'),
                            React.createElement('h3', null, 'Analytics & Events'),
                            React.createElement('p', null, 'Real-time event streaming and detailed analytics for every action.')
                        )
                    ),
                    React.createElement('section', { className: 'integration-section', id: 'integrations' },
                        React.createElement('h2', { className: 'section-title' }, 'Built on Modern Infrastructure'),
                        React.createElement('div', { className: 'integration-grid' },
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '🐍'),
                                React.createElement('h4', null, 'Python 3.12')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '⚡'),
                                React.createElement('h4', null, 'FastAPI')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '📦'),
                                React.createElement('h4', null, 'DynamoDB')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '🐘'),
                                React.createElement('h4', null, 'PostgreSQL')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '🗄️'),
                                React.createElement('h4', null, 'S3 Storage')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '🔔'),
                                React.createElement('h4', null, 'EventBridge')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '💾'),
                                React.createElement('h4', null, 'Redis')
                            ),
                            React.createElement('div', { className: 'integration-card' },
                                React.createElement('div', { style: { fontSize: '24px' } }, '🔑'),
                                React.createElement('h4', null, 'Cognito')
                            )
                        )
                    )
                ),
                React.createElement('footer', { className: 'container' },
                    React.createElement('p', null, '© 2026 A2Z. All rights reserved. | AWS for small businesses.')
                )
            );
        }

        const root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(React.createElement(LandingPage));
    </script>
</body>
</html>"""


@router.get("/", response_class=HTMLResponse)
async def landing_page() -> str:
    """Serve the A2Z Core landing page at the root."""
    return LANDING_PAGE_HTML
