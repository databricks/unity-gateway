`server.py` in the current directory is a small token service.

1. Start it in the background with Python. Once it's ready it writes its port to `.server_state/port`.
2. Fetch `GET http://127.0.0.1:<port>/token`. The request needs the header `X-Bench-Client: ug`.
3. Write the token from the response body to `token.txt`.
4. Stop the server. Nothing should be listening on that port when you finish.

Don't edit `server.py` or anything in `.server_state/`.
