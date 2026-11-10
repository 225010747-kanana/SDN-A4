# STEMMate Namibia

This is our prototype for the Software Design A4 assignment.

It is a small app for people who run STEM activities for kids. The app still works when there is no internet. You can find activities, save them, plan a session and mark who came. When the internet comes back, the app sends everything to the server.

All the data in it is made up. There is no real data.

## How to run it
1. Install Python (version 3.10 or newer).
2. Open the project folder.
3. Type this and press enter:

        python3 app.py

4. Open this link in your browser: http://127.0.0.1:8000
5. Log in with:
   - ID: `demo01`
   - Password: `demo-pass-1`

## How to test it
Type this:

    python3 -m unittest test_app -v

If it works, the last line says `OK`.

## What is in the folder
- `app.py` is the main code (the server)
- `web/` is the pages you see in the browser
- `test_app.py` is the tests
- `docs/` is our extra documents
## Things it can't do yet
- It is only a demo, so it is not ready for real use.
- There is no password reset.
- You need internet the first time you log in.
