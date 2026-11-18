// ESLint flat config (npm run lint). eslint-config-next 15.x still ships an
// eslintrc-style config, so it is loaded through FlatCompat.
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

const eslintConfig = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "tsconfig.tsbuildinfo"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default eslintConfig;
