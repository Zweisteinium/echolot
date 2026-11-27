# Sockseek (github.com/fiso64/sockseek) from a tested commit, with run.sh, which restarts the daemon when
# Echolot writes a new login. CI publishes it as lordlayer/echolot-sockseek:<TAG> and never replaces a
# published tag, so a change here or in run.sh needs a new TAG.
ARG SOCKSEEK_COMMIT=febfba1fe6125a768c99dd883e477863cee07fbf
ARG TAG=febfba1-2

FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0-alpine AS build
ARG SOCKSEEK_COMMIT
ARG TARGETARCH
RUN apk add --no-cache git
WORKDIR /src
RUN git init -q && git fetch -q --depth 1 https://github.com/fiso64/sockseek.git "$SOCKSEEK_COMMIT" \
    && git checkout -q FETCH_HEAD
RUN rid=linux-musl-$([ "$TARGETARCH" = arm64 ] && echo arm64 || echo x64) \
    && dotnet restore Sockseek.HelpGenerator/Sockseek.HelpGenerator.csproj \
    && dotnet publish Sockseek.Cli/Sockseek.Cli.csproj -c Release -r "$rid" -p:PublishSingleFile=true \
       -p:PublishTrimmed=true -p:OpenApiGenerateDocuments=false --self-contained=true -o /out \
    && rm -f /out/*.pdb

FROM alpine:3.22
ARG SOCKSEEK_COMMIT
ARG TAG
LABEL org.opencontainers.image.source="https://github.com/fiso64/sockseek" \
      org.opencontainers.image.revision="$SOCKSEEK_COMMIT" \
      org.opencontainers.image.version="$TAG"
RUN apk add --no-cache icu-libs libgcc libssl3 libstdc++ zlib tzdata
COPY --from=build /out/ /usr/bin/
COPY --from=build /src/LICENSE /usr/share/licenses/sockseek/LICENSE
COPY run.sh /run.sh
ENV HOME=/tmp
USER 1000:1000
ENTRYPOINT ["/bin/sh", "/run.sh"]
