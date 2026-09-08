# Separate demo baseline: normalize the build log before committing its layer.
FROM postgres:18.6-alpine3.24@sha256:63bdc97d67b5133bf0e5ebd500bec6d046fa851dc81340d838f0347e616107e8

ADD --checksum=sha256:161223a16f042b8e469e9441291e071464fd91d4f4bbe6f496ee8d0abd4e0701 https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libcrypto3-3.5.8-r0.apk /tmp/libcrypto3.apk
ADD --checksum=sha256:aca521e5ae4a321322a9d47ed64a1775f5ab1ffd215d1e9fc0433c58f7bfd037 https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/libssl3-3.5.8-r0.apk /tmp/libssl3.apk
RUN apk add --no-network --no-cache /tmp/libcrypto3.apk /tmp/libssl3.apk \
    && rm -f /tmp/libcrypto3.apk /tmp/libssl3.apk /usr/local/bin/gosu \
    && ! command -v gosu \
    && apk info -e 'libcrypto3=3.5.8-r0' 'libssl3=3.5.8-r0' \
 && : > /var/log/apk.log

USER 70:70
