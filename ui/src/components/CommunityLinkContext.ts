import { createContext } from 'react';

/** Returns false when there is no installed destination; the caller can open externally. */
export const CommunityLinkContext = createContext<(url: string) => boolean>(() => false);
