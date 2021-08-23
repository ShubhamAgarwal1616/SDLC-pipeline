# We expect you to replace this with your own Dockerfile! it's just here as a starting point for the CI pipeline.

FROM busybox

ENTRYPOINT ["sh", "-c", "while true; do { echo -e 'HTTP/1.1 200 OK\r\n'; echo 'Hello, World!'; } | nc -l -p 8080 2>/dev/null; done"]
