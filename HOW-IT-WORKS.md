# How Ring Trail Cam works

## One motion event

1. Ring reports motion. With a linked Ring camera, Ring sends a signed `motion_detected` webhook to `/webhooks/ring`. The app checks the `X-Signature` HMAC over the raw body before parsing anything and answers within Ring's 5 second limit. Ring retries a delivery with the same `meta.request_id`, so that id is the database key and a retry is ignored.
2. Ring's own tag decides what happens next. Following Ring's Computer Vision Guidelines for a wildlife app, events tagged `human` or `vehicle` are skipped. Every other value, including ones Ring adds later, goes on to classification.
3. The app reads the camera's configuration and stops if the video is end-to-end encrypted, because Ring can't share encrypted video. It reads the camera's coarse location (country and, in the US, state).
4. It downloads 15 seconds of video from the event time (`POST /v1/devices/{id}/media/video/download`). Ring's documentation says to wait until the requested range has ended plus 5 seconds, then retry `416` and `425` with the same request on a 5, 10, 20, 30 second backoff until 2 minutes after the range ends. The app does exactly that. If no clip ever arrives, it asks for a full resolution snapshot instead.
5. Eight frames are sampled from the clip and passed to SpeciesNet with the camera's country and state.
6. If a frame contains an animal, the clip and a thumbnail with the animal's box are kept, and a sighting is saved. Ring's own detection for the event, read from Event History, is shown next to SpeciesNet's species. If there is no animal, the clip is deleted.

## The species model

[SpeciesNet](https://github.com/google/cameratrapai) is Google's open camera trap model (Apache 2.0), trained on about 65 million camera trap images with roughly 2,500 labels. MegaDetector first finds animals, people and vehicles in the frame, and a classifier then names the species from the cropped animal, so Ring's watermark in the corner never reaches the classifier. Given a country and state, SpeciesNet rules out species that don't live there. Ring gives ISO alpha-2 country codes and SpeciesNet expects alpha-3, so the app converts them. When frames disagree, the best species-level answer wins over a genus or family answer.

## Simulation with the Ring Developer Playground

A Playground token isn't tied to an app or an account link, so Ring has no webhook to send Playground events to. Ring documents polling Event History as the alternative, so while a Playground token is set the app polls `GET /v1/history/devices/{id}/events` every 5 seconds and reads each event's tags by repeating the query once per `event_types` filter (`motion.package_delivery`, `motion.vehicle`, `motion.animal`, `motion.human`, `motion.other_motion`). An event can carry more than one tag, so a package tag means the Package button and a vehicle tag means the Vehicle button; anything else is the Motion button. The tags Ring gave each event are shown in the Ring events list. They describe the Playground's own video, so for Motion they play no part in the result: the clip is replaced and SpeciesNet decides what is in it. New events go through the same pipeline as webhooks.

Every event from the Playground's Motion button uses a random clip from `simulation/motion-clips` instead of the Playground's own video, whatever Ring tagged that video as. Vehicle events are skipped, and Package events use the Playground's own video. The pool is unlabeled trail camera footage of animals and people, so SpeciesNet decides what is in each clip, and the card is marked "Simulated clip".

## Connecting a Ring camera

The app connects through a private app you create in the Ring developer console. That app's Account linking settings point at four addresses served through a Cloudflare quick tunnel:

- **Account Link URL** (`/ring/link`) is the sign-in page Ring sends you to. Ring requires a sign-in or create-account step here before the link is claimed.
- **Default Redirect URL** (`/ring/link/done`) is where Ring sends you afterwards.
- **Token Exchange URL** (`/ring/token-exchange`) receives the authorization code Ring posts server to server. The code is exchanged at `oauth.ring.com` within its 60 second life.
- **Webhook URL** (`/webhooks/ring`) receives motion events.

Linking follows Ring's one-way account linking. The app holds the exchanged tokens as unclaimed. When you sign in on the link page, it recomputes Ring's nonce (HMAC-SHA256 of `time:account_id`, URL-safe base64) against each unclaimed token's account. On a match it confirms the link with `POST` and then `PATCH /v1/accounts/me/app-integrations`. From then on it refreshes the access token as needed.

Through the tunnel, only those four addresses answer. The dashboard, clips and settings answer on your own computer only.

Ring's documentation doesn't show the exact body it posts to the Token Exchange URL, so that endpoint accepts the code as JSON, a form field or a query parameter.

## Files

| Path | What it is |
|---|---|
| `start.py`, `stop.py` | Install, start in the background, open the dashboard; stop |
| `trailcam/app.py` | Web app: webhook, dashboard API, mode and keys, account linking |
| `trailcam/ring.py` | Ring Partner API client, webhook signature and nonce checks, tokens |
| `trailcam/poller.py` | Event History polling for the Playground |
| `trailcam/pipeline.py` | Background workers: encryption check, download, classify, store |
| `trailcam/classify.py` | Frame sampling and SpeciesNet |
| `trailcam/tunnel.py` | Cloudflare quick tunnel for camera mode |
| `trailcam/store.py` | SQLite tables for events and sightings |
| `trailcam/static/index.html` | The dashboard |
