#!/usr/bin/env node

"use strict";

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const zlib = require("zlib");

const mockttpModule =
    process.env.NOTEBOOKLM_MOCKTTP_MODULE || "/tmp/notebooklm-mockttp/node_modules/mockttp";
const { getLocal } = require(mockttpModule);

const host = "notebooklm-pa.googleapis.com";
const upstreamIp = process.env.NOTEBOOKLM_UPSTREAM_IP;
const outputDir = process.env.NOTEBOOKLM_CAPTURE_DIR || "/tmp/notebooklm-mobile-grpc";
const listenPort = Number(process.env.NOTEBOOKLM_CAPTURE_PORT || "8081");
const httpToolkitConfig =
    process.env.HTTPTOOLKIT_CONFIG_DIR ||
    path.join(os.homedir(), "Library", "Preferences", "httptoolkit");
const requests = new Map();
let nextId = 0;

if (!upstreamIp) {
    throw new Error("NOTEBOOKLM_UPSTREAM_IP is required; resolve it through DNS-over-HTTPS");
}

fs.mkdirSync(outputDir, { recursive: true, mode: 0o700 });
fs.chmodSync(outputDir, 0o700);

function grpcFrames(body, encoding) {
    const frames = [];
    let offset = 0;
    while (offset < body.length) {
        if (offset + 5 > body.length) throw new Error(`truncated header at ${offset}`);
        const compressed = body[offset];
        const size = body.readUInt32BE(offset + 1);
        offset += 5;
        if (offset + size > body.length) throw new Error(`truncated message at ${offset}`);
        let frame = body.subarray(offset, offset + size);
        offset += size;
        if (compressed) {
            if (encoding !== "gzip") throw new Error(`unsupported encoding: ${encoding}`);
            frame = zlib.gunzipSync(frame);
        }
        frames.push(frame);
    }
    return frames;
}

function dump(id, method, direction, body, encoding, extra = {}) {
    let frames = [];
    let frameError;
    try {
        frames = grpcFrames(body, encoding);
    } catch (error) {
        frameError = error.message;
    }

    const safeMethod = method.replace(/[^A-Za-z0-9_.-]/g, "_");
    const files = frames.map((frame, index) => {
        const filename = `${id}_${safeMethod}.${direction}.${index}.pb`;
        fs.writeFileSync(path.join(outputDir, filename), frame, { mode: 0o600 });
        return { file: filename, size: frame.length };
    });

    let rawFile;
    if (frameError) {
        rawFile = `${id}_${safeMethod}.${direction}.raw`;
        fs.writeFileSync(path.join(outputDir, rawFile), body, { mode: 0o600 });
    }

    const record = {
        id,
        method,
        direction,
        frames: files,
        frame_error: frameError,
        raw_file: rawFile,
        ...extra,
    };
    fs.appendFileSync(path.join(outputDir, "index.jsonl"), `${JSON.stringify(record)}\n`, {
        mode: 0o600,
    });
    console.log(
        `${id} ${direction} ${method} ${files.map((file) => file.size).join(",") || "empty"}`
    );
}

async function main() {
    const server = getLocal({
        https: {
            keyPath: path.join(httpToolkitConfig, "ca.key"),
            certPath: path.join(httpToolkitConfig, "ca.pem"),
        },
        http2: true,
        recordTraffic: false,
        maxBodySize: 100 * 1024 * 1024,
    });

    await server.start(listenPort);
    await server.on("request", (request) => {
        if (
            request.destination.hostname !== host ||
            !String(request.headers["content-type"]).startsWith("application/grpc")
        ) {
            return;
        }
        const id = String(++nextId).padStart(3, "0");
        const method = request.path.split("/").pop();
        requests.set(request.id, { id, method });
        dump(id, method, "request", request.body.buffer, request.headers["grpc-encoding"], {
            path: request.path,
            http_version: request.httpVersion,
        });
    });
    await server.on("response", (response) => {
        const request = requests.get(response.id);
        if (!request) return;
        dump(
            request.id,
            request.method,
            "response",
            response.body.buffer,
            response.headers["grpc-encoding"],
            {
                status_code: response.statusCode,
                grpc_status:
                    response.trailers["grpc-status"] || response.headers["grpc-status"],
            }
        );
        requests.delete(response.id);
    });

    await server.forAnyRequest().forHostname(host).thenPassThrough({
        transformRequest: {
            replaceHost: { targetHost: `${upstreamIp}:443`, updateHostHeader: false },
        },
        ignoreHostHttpsErrors: true,
    });
    await server.forUnmatchedRequest().thenPassThrough();

    console.log(`Capturing ${host} gRPC on :${listenPort}; output: ${outputDir}`);
    console.warn("Captured protobuf bodies can contain private notebook data. Do not commit them.");
    for (const signal of ["SIGINT", "SIGTERM"]) {
        process.once(signal, async () => {
            await server.stop();
            process.exit(0);
        });
    }
}

assert.deepStrictEqual(grpcFrames(Buffer.from("0000000003616263", "hex")), [
    Buffer.from("abc"),
]);
main().catch((error) => {
    console.error(error);
    process.exit(1);
});
