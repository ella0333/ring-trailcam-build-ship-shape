# Friction log

Things that slowed the build down, written as they happened.

## 2026-10-13

The starter repo (AmazonAppDev/ring-api-helloworld) links to separate doc pages such as notifications.html, authentication.html, app-registration.html and device-discovery.html under developer.amazon.com/docs/ring/. All four return 404. The content now lives in the single api-documentation.html page, which is close to 1 MB of HTML.

Ring ships no SDK and no local simulator, so testing a webhook-driven app without a device meant using a community emulator (ring-sandbox). It could only serve one fixed clip per camera, so every simulated event showed the same footage.

The Ring Developer Playground isn't tied to an app, so its simulated events never reach an app's webhook. The docs don't say this anywhere; it follows from webhooks being delivered per app and per linked account. We read Playground events from Event History instead.

The documentation describes the server-to-server call Ring makes to the partner's Token Exchange URL, but never shows its request body. We made that endpoint accept the authorization code in three different shapes.

Partner-initiated OAuth (a "Connect Ring account" button in the partner's own app) is invitation only. A hackathon app therefore has to use one-way account linking, which starts from the Ring AppStore and requires a sign-in page on the partner side.

## 2026-10-14

The Developer Portal's Account linking page names four fields (Account Link URL, Default Redirect URL, Token Exchange URL, Webhook URL), while the API documentation's Application Deployment section lists a different set (Token Exchange URL, Account Link URL, Webhook URL, App Homepage URL). We followed the portal guide.

Event History withholds a motion event's subtype from the response. The only way to learn whether Ring saw a person, a vehicle or an animal is to repeat the query once per subtype filter and see which result contains the event.
