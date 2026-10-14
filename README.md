# Ring Trail Cam

Built for and submitted to the [Build, Ship, Shape: Amazon Developer Hackathon](https://amazonappdev2026.devpost.com/) (Ring track).

Ring Trail Cam turns a Ring camera into a wildlife trail cam. When Ring reports motion that it did not tag as a person or a vehicle, the app downloads the clip through the Ring API, runs Google's SpeciesNet model on it, and adds the animal to a field journal in your browser. Clips with no animal in them are deleted.

You can run it two ways: as a simulation with the Ring Developer Playground, which needs no Ring camera, or with your own Ring camera.

## What you need

- Python 3.11 or newer.
- A Ring developer account, from the [Ring developer console](https://developer.amazon.com/ring/console/apps).
- For your own camera, also: a private app in the Ring developer console with the Cameras and Doorbells scope, a Ring camera in the US, and a Ring Protect plan or trial on that Ring account.

## Start and stop

```
python start.py
```

The first run installs what it needs, which takes a few minutes. The dashboard then opens at http://localhost:8000 and asks how you want to use it.

```
python stop.py
```

## Simulation with the Ring Developer Playground

Choose Simulation on the dashboard. It links to the [Ring Developer Playground](https://developer.amazon.com/ring/console/playground), where you sign in, click Generate Token and paste the token into the dashboard. Then press Package, Vehicle or Motion in the Playground, and each event shows up on the dashboard within a few seconds.

- **Motion** uses a random clip from `simulation/motion-clips` in place of the Playground's own video, which is Ring's synthetic doorbell. The clips are real trail camera footage of animals and people, with no labels, and SpeciesNet decides what is in each one. These cards are marked "Simulated clip".
- **Package** is processed with the Playground's own video.
- **Vehicle** is skipped, because Ring tagged it as a vehicle.

A Playground token lasts about 30 minutes. The dashboard shows the time left and asks for a new token when it runs out.

## Your own Ring camera

Choose My Ring camera on the dashboard. It walks you through three steps:

1. Enter your app's Client ID, Client Secret and HMAC Signature Key.
2. The app opens a secure tunnel and shows four addresses. Paste them into your app's Account linking settings in the Ring developer console.
3. Open Connect on your app's page in the console, choose Log in with Ring, and sign in with the Ring account that has your camera.

From then on, every motion clip from your camera is checked for animals. The tunnel address changes each time the app starts, so update the addresses in the console when it does. The dashboard shows them under "Ring console addresses".

## More

[HOW-IT-WORKS.md](HOW-IT-WORKS.md) explains the pipeline and the Ring API calls. Clip sources are in [simulation/CREDITS.md](simulation/CREDITS.md).

MIT licensed.
