# Copyright (c) 2022 Cisco Systems, Inc. and others.
# All rights reserved.
FROM debian:stable-slim AS build

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /ws

# Install the various depends
RUN apt-get update
RUN apt-get install -y openjdk-17-jdk-headless maven
RUN mkdir -p /usr/share/man/man1/ \
    && apt-get -y install git gcc g++ libboost-dev cmake libssl-dev libsasl2-dev \
         curl wget libgss-dev liblz4-dev libzstd-dev

# Build/install zlib - zlib1g-dev does not work for static builds of librdkafka
RUN cd /tmp && git clone https://github.com/madler/zlib.git \
    && cd zlib \
    && git checkout v1.2.12 \
    && CFLAGS=-fPIC  ./configure --static \
    && make install

# Install zstd v1.5.2 with -fPIC manually
RUN cd /tmp && \
    git clone --branch v1.5.2 https://github.com/facebook/zstd.git && \
    cd zstd && \
    make CFLAGS="-fPIC" && make prefix=/usr/local install && \
    rm -f /usr/lib/x86_64-linux-gnu/libzstd.a

# Build/install librdkafka
# NOTE: Installed under /usr/local/lib
RUN cd /tmp && git clone https://github.com/edenhill/librdkafka.git \
    && cd librdkafka \
    && git checkout v1.9.2 \
    && ./configure --enable-static --disable-curl \
    && make \
    && make install

# Build/install yaml-cpp
RUN cd /tmp && \
    git clone https://github.com/jbeder/yaml-cpp.git && \
    cd yaml-cpp && \
    git checkout yaml-cpp-0.7.0 && \
    mkdir build && cd build && \
    cmake -DYAML_BUILD_SHARED_LIBS=OFF -DYAML_CPP_BUILD_TESTS=OFF .. && \
    make && make install

# Clean up
RUN rm -rf /tmp/* && apt-get clean