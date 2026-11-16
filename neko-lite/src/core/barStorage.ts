// Price history kept in the browser's IndexedDB between visits. Each market
// is one record of plain number arrays (about 200 kB for 5000 bars), so a
// second visit downloads only the bars since the first.

import type { BarStorage } from "./runtime";
import type { StoredBars } from "./history";

const DATABASE = "neko-lite";
const STORE = "bars";

/** IndexedDB-backed storage, or null where the browser has none (or in tests). */
export function browserStorage(): BarStorage | null {
  if (typeof indexedDB === "undefined") return null;
  let opening: Promise<IDBDatabase> | null = null;
  const open = () => {
    opening ??= new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(DATABASE, 1);
      request.onupgradeneeded = () => { request.result.createObjectStore(STORE); };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("storage is blocked"));
    });
    return opening;
  };
  const run = async <T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> => {
    const db = await open();
    return new Promise<T>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const request = work(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  };
  return {
    get: async (key) => (await run<StoredBars | undefined>("readonly", (store) => store.get(key))) ?? null,
    put: async (key, bars) => { await run("readwrite", (store) => store.put(bars, key)); },
  };
}
