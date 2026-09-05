// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  reactStrictMode: true,
  transpilePackages: ["web-shared"],
};

export default nextConfig;
