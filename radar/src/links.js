export const isUnderBaseUrl = (url, baseUrl) => typeof url === 'string' && url.startsWith(`${baseUrl}/`);

export function isHttpsUrl(url) {
  try {
    return new URL(url).protocol === 'https:';
  } catch {
    return false;
  }
}

export function failureUrl(failure, baseUrl) {
  if (failure.kind === 'external') {
    return isHttpsUrl(failure.webUrl) ? failure.webUrl : null;
  }
  return isUnderBaseUrl(failure.webUrl, baseUrl) ? failure.webUrl : baseUrl;
}
