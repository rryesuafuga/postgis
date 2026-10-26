Optional: if your network inspects TLS (some office proxies do) and `pip install` or the data
downloads fail with certificate errors inside the tools image, copy the proxy's CA certificate
here as a `.crt` file (PEM) and rebuild: `docker compose --profile tools build`.
`.crt` files in this folder are git-ignored.
