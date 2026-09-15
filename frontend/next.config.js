/** @type {import('next').NextConfig} */
const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:18000";
const desktopStatic = process.env.DESKTOP_STATIC_EXPORT === "1";

// WB-001: isolate dev and build output so `next build` can never corrupt the
// running `next dev` cache (mixed .next artifacts broke hydration -> permanent
// "Loading dashboard..."). The npm scripts set NEXT_DIST_DIR explicitly via
// cross-env (.next-dev for dev, .next-prod for build/start). An explicit value
// is required because Next.js resets NODE_ENV during its internal build phases
// (e.g. static generation), which would otherwise write into the dev directory.
const distDir = process.env.NEXT_DIST_DIR || ".next";

const nextConfig = {
  reactStrictMode: true,
  // The Windows installer runs the prebuilt Next server directly with the
  // bundled Node runtime. Keep this enabled so build-runtime.ps1 can package
  // .next-prod/standalone/server.js instead of relying on customer npm files.
  output: desktopStatic ? "export" : "standalone",
  trailingSlash: desktopStatic,
  images: { unoptimized: true },
  distDir,
  ...(desktopStatic ? {} : { async rewrites() { return [{ source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` }]; } }),
};

module.exports = nextConfig;
