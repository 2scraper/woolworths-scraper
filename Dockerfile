# Builds the Playwright engine (the one the README recommends) into a
# container.
#
#   docker build -t woolworths-scraper .
#   docker run --rm -v "$PWD/out:/out" woolworths-scraper \
#     --url "https://www.woolworths.com.au/shop/search/products?searchTerm=milk" \
#     --pages 3 --out /out/milk
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential, and .dockerignore
# keeps one out of the build context. A .env baked into an image is a
# credential published to everyone who can pull it.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./

# `--with-deps` pulls Chromium's shared-library dependencies through apt;
# they are not pip packages and cannot ride in requirements.txt.
#
# Chromium rather than Chrome: this site was measured serving Playwright's
# bundled Chromium the full catalogue on every headful fetch, so there is
# nothing to buy by shipping several hundred MB more.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    && playwright install --with-deps chromium

# XVFB, AND IT IS NOT OPTIONAL ON THIS SITE.
#
# Every other image in this family runs the engine `--headless`, because a
# container has no display. That does not work here and the measurement is
# unambiguous: from one datacentre address on 2026-09-16, four URLs fetched
# HEADFUL were served HTTP 200 and the full catalogue 4 of 4, and the same
# four fetched HEADLESS got Akamai's 403 "Access Denied" page 4 of 4.
#
# So the container gives the browser a virtual display and runs it headful.
# An image that ran headless here would build, start, print --help happily,
# and then return exit 3 on every real invocation — which is precisely the
# "reports success while doing less than it says" shape this family keeps
# finding (§16), arriving by way of a default nobody re-measured.
# `xauth` alongside `xvfb`, and it is not optional either: `xvfb-run` shells
# out to xauth to create the display's authority file and dies with
# "xauth command not found" without it — on EVERY invocation, `--help`
# included. Found by running the image rather than by building it, which is
# the whole reason CI does both (CLAUDE.md §11).
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth x11-utils \
    && rm -rf /var/lib/apt/lists/*

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: three repos in this family
# shipped an image missing proxy_pool.py, which the engine imports at module
# level, so it died with ModuleNotFoundError on every invocation INCLUDING
# `--help` — a broken container that nothing in the repo would have noticed.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py docker-entrypoint.sh ./
RUN chmod +x /app/docker-entrypoint.sh

ENV WOOLWORTHS_DOCKER=1

# The entrypoint starts Xvfb on a fixed display and execs the scraper; see
# docker-entrypoint.sh for why it does that rather than using `xvfb-run`.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--help"]
