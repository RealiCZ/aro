/// <reference types="svelte" />
/// <reference types="vite/client" />

import type { TreeData } from './lib/types';

declare global {
  interface Window {
    __ARO_DATA__?: TreeData;
  }
}

export {};
