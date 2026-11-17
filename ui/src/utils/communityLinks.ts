export type CommunitySite = 'linuxdo' | 'reddit';

export const COMMUNITY_SITES = {
  linuxdo: { pluginId: 'plugin-linuxdo', title: 'Linux.do', home: 'https://linux.do/' },
  reddit: { pluginId: 'plugin-reddit', title: 'Reddit', home: 'https://www.reddit.com/' },
} as const;

export interface CommunityNavigation {
  site: CommunitySite;
  url: string;
  requestId: number;
}

/** Parse hosts, never match a domain substring inside an arbitrary URL. */
export function resolveCommunityLink(href: string): { site: CommunitySite; url: string } | null {
  try {
    const url = new URL(href);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.port) return null;
    const host = url.hostname;
    const site = host === 'linux.do' || host.endsWith('.linux.do') ? 'linuxdo'
      : host === 'reddit.com' || host.endsWith('.reddit.com') || host === 'redd.it' ? 'reddit' : null;
    if (!site) return null;
    url.protocol = 'https:';
    return { site, url: url.href };
  } catch { return null; }
}
