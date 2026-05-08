#!/usr/bin/env python3
"""
Check flatpak runtime updates for packages from ublue-os/bluefin system-flatpaks.list
and create GitHub issues for outdated packages.
"""

import os
import re
import subprocess
import sys
import json
import logging
from typing import Dict, List, Set, Optional, Tuple, NamedTuple
from dataclasses import dataclass
import requests
import yaml


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class FlatpakInfo:
    """Information about a flatpak package from multiple sources."""
    flatpak_id: str
    sources: List[str]  # ['bluefin', 'bazzite-gnome', 'aurora', etc.]
    runtime_info: Optional[Dict] = None
    current_runtime: Optional[str] = None


class FlatpakRuntimeChecker:
    # Well-maintained apps that reliably track the latest stable version of each
    # runtime family. Querying their Flathub metadata tells us the current stable
    # runtime version without depending on the flatpak CLI or hardcoded constants.
    RUNTIME_SENTINELS = {
        'org.gnome.Platform':      ['org.gnome.Calculator', 'org.gnome.Calendar'],
        'org.kde.Platform':        ['org.kde.kalk', 'org.kde.gwenview'],
        'org.freedesktop.Platform': ['com.github.tchx84.Flatseal', 'org.videolan.VLC'],
    }

    # Last-resort fallback versions. Update these when a new stable runtime ships
    # and Flathub API queries are temporarily unavailable (e.g. network outage).
    FALLBACK_RUNTIME_VERSIONS = {
        'org.gnome.Platform':      '49',
        'org.freedesktop.Platform': '25.08',
        'org.kde.Platform':        '6.10',
    }

    def __init__(self, output_file: str = None):
        self.flathub_base_url = "https://flathub.org/api/v2/appstream"
        self.output_file = output_file or "outdated_packages.json"
        self._runtime_version_cache: Dict[str, List[str]] = {}
        
    def fetch_flatpak_list(self) -> Dict[str, FlatpakInfo]:
        """Fetch and merge flatpak lists from multiple ublue-os sources with deduplication."""
        
        # Define all sources with their URLs and formats
        sources = {
            'bluefin': {
                'url': 'https://raw.githubusercontent.com/projectbluefin/common/main/system_files/bluefin/usr/share/ublue-os/homebrew/system-flatpaks.Brewfile',
                'format': 'brewfile'  # flatpak "app.id" per line
            },
            'aurora': {
                'url': 'https://raw.githubusercontent.com/get-aurora-dev/common/main/system_files/shared/usr/share/ublue-os/homebrew/system-flatpaks.Brewfile',
                'format': 'brewfile'  # flatpak "app.id" per line
            },
            'bazzite-gnome': {
                'url': 'https://raw.githubusercontent.com/ublue-os/bazzite/main/installer/gnome_flatpaks/flatpaks',
                'format': 'full_ref'  # app/package/arch/branch format
            },
            'bazzite-kde': {
                'url': 'https://raw.githubusercontent.com/ublue-os/bazzite/main/installer/kde_flatpaks/flatpaks',
                'format': 'full_ref'  # app/package/arch/branch format
            },
            # Bazaar curated sources
            'bluefin-bazaar': {
                'url': 'https://raw.githubusercontent.com/projectbluefin/common/main/system_files/bluefin/etc/bazaar/curated.yaml',
                'format': 'curated_yaml'  # YAML with rows -> sections -> category.appids
            },
            'aurora-bazaar': {
                'url': 'https://raw.githubusercontent.com/get-aurora-dev/common/main/system_files/shared/etc/bazaar/curated.yaml',
                'format': 'curated_yaml'  # YAML with rows -> sections -> category.appids
            },
            'bazzite-bazaar': {
                'url': 'https://raw.githubusercontent.com/ublue-os/bazzite/main/system_files/desktop/shared/usr/share/ublue-os/bazaar/config.yaml',
                'format': 'bazaar_yaml'  # YAML format with appids in sections
            }
        }
        
        # Dictionary to store deduplicated flatpaks with source tracking
        flatpak_dict = {}
        
        for source_name, source_config in sources.items():
            logger.info(f"Fetching flatpaks from {source_name}")
            
            try:
                response = requests.get(source_config['url'], timeout=30)
                response.raise_for_status()
                
                source_flatpaks = []
                
                if source_config['format'] == 'bazaar_yaml':
                    # Parse YAML and extract appids from all sections (bazzite legacy format)
                    source_flatpaks = self._parse_bazaar_yaml(response.text)
                elif source_config['format'] == 'curated_yaml':
                    # Parse new bazaar curated.yaml (rows -> sections -> category.appids)
                    source_flatpaks = self._parse_curated_yaml(response.text)
                elif source_config['format'] == 'brewfile':
                    # Parse Brewfile format: flatpak "app.id" per line
                    source_flatpaks = self._parse_brewfile(response.text)
                else:
                    # Handle plain list formats (full_ref for bazzite)
                    for line in response.text.strip().split('\n'):
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if source_config['format'] == 'full_ref':
                                # Extract package ID: app/package.id/arch/branch -> app/package.id
                                parts = line.split('/')
                                if len(parts) >= 2:
                                    flatpak_id = f"{parts[0]}/{parts[1]}"
                                else:
                                    flatpak_id = line
                            else:
                                flatpak_id = line
                            
                            # Only include app flatpaks (not runtimes)
                            if flatpak_id.startswith('app/'):
                                source_flatpaks.append(flatpak_id)
                
                logger.info(f"Found {len(source_flatpaks)} flatpaks from {source_name}")
                
                # Add to deduplicated dictionary
                for flatpak_id in source_flatpaks:
                    if flatpak_id in flatpak_dict:
                        # Flatpak already exists, add this source
                        flatpak_dict[flatpak_id].sources.append(source_name)
                    else:
                        # New flatpak
                        flatpak_dict[flatpak_id] = FlatpakInfo(
                            flatpak_id=flatpak_id,
                            sources=[source_name]
                        )
                        
            except requests.RequestException as e:
                logger.error(f"Failed to fetch flatpak list from {source_name}: {e}")
                # Continue with other sources instead of failing completely
                continue
            except Exception as e:
                logger.error(f"Error processing {source_name}: {e}")
                # Continue with other sources instead of failing completely
                continue
        
        total_unique = len(flatpak_dict)
        total_sources = sum(len(info.sources) for info in flatpak_dict.values())
        logger.info(f"Combined total: {total_unique} unique flatpaks from {total_sources} source entries")
        
        # Log some statistics
        source_counts = {}
        for info in flatpak_dict.values():
            for source in info.sources:
                source_counts[source] = source_counts.get(source, 0) + 1
        
        for source, count in source_counts.items():
            logger.info(f"  {source}: {count} flatpaks")
        
        return flatpak_dict
    
    def _parse_bazaar_yaml(self, yaml_content: str) -> List[str]:
        """Parse bazaar config YAML and extract all appids from all sections."""
        try:
            # Parse the YAML content
            config = yaml.safe_load(yaml_content)
            
            flatpaks = []
            
            # The bazaar config has a 'sections' key containing a list of sections
            if isinstance(config, dict) and 'sections' in config:
                sections = config['sections']
                if isinstance(sections, list):
                    for section in sections:
                        if isinstance(section, dict) and 'appids' in section:
                            appids = section['appids']
                            if isinstance(appids, list):
                                for app_id in appids:
                                    if isinstance(app_id, str) and app_id.strip():
                                        # Convert to app/package.id format
                                        app_id = app_id.strip()
                                        if not app_id.startswith('app/'):
                                            app_id = f"app/{app_id}"
                                        flatpaks.append(app_id)
            
            logger.debug(f"Parsed {len(flatpaks)} flatpaks from bazaar YAML")
            return flatpaks
            
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse YAML: {e}")
            return []
        except Exception as e:
            logger.error(f"Error processing bazaar YAML: {e}")
            return []
    
    def _parse_brewfile(self, content: str) -> List[str]:
        """Parse Brewfile format and extract flatpak app IDs.
        
        Handles lines like: flatpak "com.example.App"
        """
        flatpaks = []
        for line in content.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Match: flatpak "app.id"
            if line.startswith('flatpak '):
                app_id = line[len('flatpak '):].strip().strip('"\'')
                if app_id:
                    flatpaks.append(f"app/{app_id}")
        logger.debug(f"Parsed {len(flatpaks)} flatpaks from Brewfile")
        return flatpaks

    def _parse_curated_yaml(self, yaml_content: str) -> List[str]:
        """Parse new bazaar curated.yaml format and extract all appids.
        
        Structure: rows -> sections -> category.appids (list of app IDs without app/ prefix)
        """
        try:
            config = yaml.safe_load(yaml_content)
            flatpaks = []

            if not isinstance(config, dict) or 'rows' not in config:
                logger.warning("Unexpected curated.yaml structure: missing 'rows' key")
                return []

            for row in config['rows']:
                if not isinstance(row, dict):
                    continue
                for section in row.get('sections', []):
                    if not isinstance(section, dict):
                        continue
                    category = section.get('category', {})
                    if not isinstance(category, dict):
                        continue
                    for app_id in category.get('appids', []):
                        if isinstance(app_id, str) and app_id.strip():
                            app_id = app_id.strip()
                            if not app_id.startswith('app/'):
                                app_id = f"app/{app_id}"
                            flatpaks.append(app_id)

            logger.debug(f"Parsed {len(flatpaks)} flatpaks from curated YAML")
            return flatpaks

        except yaml.YAMLError as e:
            logger.error(f"Failed to parse curated YAML: {e}")
            return []
        except Exception as e:
            logger.error(f"Error processing curated YAML: {e}")
            return []

    def get_app_flatpaks(self, flatpak_dict: Dict[str, FlatpakInfo]) -> Dict[str, FlatpakInfo]:
        """Filter to get only app flatpaks (not runtimes) - all should already be apps."""
        return {fid: info for fid, info in flatpak_dict.items() if fid.startswith('app/')}
    
    def get_flatpak_info(self, flatpak_id: str) -> Optional[Dict]:
        """Get flatpak information from Flathub API."""
        # Remove 'app/' prefix for API call
        app_id = flatpak_id.replace('app/', '')
        
        try:
            response = requests.get(f"{self.flathub_base_url}/{app_id}", timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Could not fetch info for {app_id}: HTTP {response.status_code}")
                return None
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch info for {app_id}: {e}")
            return None
    
    def get_runtime_from_flatpak_info(self, flatpak_info: Dict) -> Optional[str]:
        """Extract runtime information from flatpak metadata."""
        try:
            # Look for runtime in bundle information
            if 'bundle' in flatpak_info:
                bundle = flatpak_info['bundle']
                if 'runtime' in bundle:
                    return bundle['runtime']
            
            # Alternative: check in metadata
            if 'metadata' in flatpak_info:
                metadata = flatpak_info['metadata']
                if 'runtime' in metadata:
                    return metadata['runtime']
                    
            return None
        except (KeyError, TypeError) as e:
            logger.debug(f"Could not extract runtime info: {e}")
            return None
    
    def get_available_runtime_versions(self, runtime_name: str) -> List[str]:
        """Get the latest version of a runtime, querying Flathub first.

        Strategy:
          1. Return cached result if already resolved this session.
          2. Query sentinel apps on Flathub — apps that are actively maintained
             and always updated to the latest stable runtime. Extract the runtime
             version they declare and take the maximum across all sentinels.
          3. Try the local flatpak CLI (works when flathub remote is configured).
          4. Fall back to FALLBACK_RUNTIME_VERSIONS (hardcoded safety net).
        """
        if runtime_name in self._runtime_version_cache:
            return self._runtime_version_cache[runtime_name]

        # --- Tier 1: sentinel apps via Flathub API ---
        sentinel_apps = self.RUNTIME_SENTINELS.get(runtime_name, [])
        versions_found = []

        for app_id in sentinel_apps:
            try:
                response = requests.get(f"{self.flathub_base_url}/{app_id}", timeout=30)
                if response.status_code != 200:
                    continue
                app_info = response.json()
                runtime_ref = None
                if 'bundle' in app_info and 'runtime' in app_info['bundle']:
                    runtime_ref = app_info['bundle']['runtime']
                elif 'metadata' in app_info and 'runtime' in app_info['metadata']:
                    runtime_ref = app_info['metadata']['runtime']

                if runtime_ref:
                    # runtime_ref looks like "org.gnome.Platform/x86_64/49"
                    ref_parts = runtime_ref.split('/')
                    if len(ref_parts) >= 3 and ref_parts[0] == runtime_name:
                        versions_found.append(ref_parts[2])
                        logger.info(f"Detected {runtime_name} {ref_parts[2]} from sentinel {app_id}")
                        break  # One confirmed answer is enough
            except requests.RequestException as e:
                logger.debug(f"Could not query sentinel app {app_id}: {e}")

        if versions_found:
            latest = max(versions_found, key=lambda v: [int(x) for x in v.replace('-', '.').split('.') if x.isdigit()] or [0])
            result = [latest]
            self._runtime_version_cache[runtime_name] = result
            return result

        # --- Tier 2: local flatpak CLI ---
        try:
            cmd = ['flatpak', 'remote-ls', '--runtime', 'flathub', '--columns=name,version', runtime_name]
            result_proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result_proc.returncode == 0:
                versions = []
                for line in result_proc.stdout.strip().split('\n'):
                    if line.strip():
                        parts = line.split('\t')
                        if len(parts) >= 2 and parts[0].strip() == runtime_name:
                            versions.append(parts[1].strip())
                if versions:
                    self._runtime_version_cache[runtime_name] = versions
                    return versions
        except Exception as e:
            logger.debug(f"Flatpak command failed for {runtime_name}: {e}")

        # --- Tier 3: hardcoded fallback ---
        if runtime_name in self.FALLBACK_RUNTIME_VERSIONS:
            fallback = self.FALLBACK_RUNTIME_VERSIONS[runtime_name]
            logger.warning(
                f"All dynamic lookups failed for {runtime_name}; "
                f"using hardcoded fallback version {fallback}. "
                f"Update FALLBACK_RUNTIME_VERSIONS if this is stale."
            )
            result = [fallback]
            self._runtime_version_cache[runtime_name] = result
            return result

        logger.warning(f"Could not determine latest version for runtime {runtime_name}")
        return []
    
    def compare_versions(self, current: str, latest: str) -> bool:
        """Compare version strings to determine if current is outdated."""
        try:
            # Simple version comparison for common patterns
            # This is a basic implementation - real version comparison is complex
            current_parts = [int(x) for x in current.split('.') if x.isdigit()]
            latest_parts = [int(x) for x in latest.split('.') if x.isdigit()]
            
            # Pad shorter version with zeros
            max_len = max(len(current_parts), len(latest_parts))
            current_parts.extend([0] * (max_len - len(current_parts)))
            latest_parts.extend([0] * (max_len - len(latest_parts)))
            
            return current_parts < latest_parts
        except (ValueError, TypeError):
            # If we can't parse versions, assume string comparison
            return current != latest
    
    def save_outdated_packages(self, outdated_packages: List[Dict], all_tracked_flatpaks: Dict[str, any]):
        """Save outdated packages to JSON file for issue generation."""
        # Convert all tracked flatpaks to a list for easier processing
        all_tracked_list = list(all_tracked_flatpaks.keys())
        
        output_data = {
            "timestamp": __import__('datetime').datetime.now().isoformat(),
            "total_checked": getattr(self, '_total_checked', 0),
            "outdated_count": len(outdated_packages),
            "outdated_packages": outdated_packages,
            "all_tracked_packages": all_tracked_list
        }
        
        try:
            with open(self.output_file, 'w') as f:
                json.dump(output_data, f, indent=2)
            logger.info(f"Saved {len(outdated_packages)} outdated packages to {self.output_file}")
            logger.info(f"Total tracked packages: {len(all_tracked_list)}")
        except Exception as e:
            logger.error(f"Failed to save outdated packages: {e}")
            sys.exit(1)
    
    def check_runtime_updates(self):
        """Main method to check for runtime updates and save outdated packages to JSON."""
        logger.info("Starting flatpak runtime update check")
        
        # Fetch flatpak dictionary from multiple sources
        flatpak_dict = self.fetch_flatpak_list()
        app_flatpaks = self.get_app_flatpaks(flatpak_dict)
        
        logger.info(f"Checking {len(app_flatpaks)} unique app flatpaks for runtime updates")
        self._total_checked = len(app_flatpaks)
        
        outdated_packages = []
        
        for flatpak_id, flatpak_info in app_flatpaks.items():
            logger.info(f"Checking {flatpak_id} (from: {', '.join(flatpak_info.sources)})")
            
            # Get flatpak information
            runtime_info = self.get_flatpak_info(flatpak_id)
            if not runtime_info:
                logger.warning(f"Could not get info for {flatpak_id}, skipping")
                continue
            
            # Store runtime info in our data structure for potential future use
            flatpak_info.runtime_info = runtime_info
            
            # Extract runtime information
            current_runtime = self.get_runtime_from_flatpak_info(runtime_info)
            if not current_runtime:
                logger.warning(f"Could not determine runtime for {flatpak_id}, skipping")
                continue
            
            # Store current runtime info
            flatpak_info.current_runtime = current_runtime
            
            logger.info(f"{flatpak_id} uses runtime: {current_runtime}")
            
            # Get available runtime versions
            runtime_name = current_runtime.split('/')[0] if '/' in current_runtime else current_runtime
            available_versions = self.get_available_runtime_versions(runtime_name)
            
            if not available_versions:
                logger.warning(f"Could not get available versions for runtime {runtime_name}")
                continue
            
            # Find the latest version
            latest_version = max(available_versions) if available_versions else None
            if not latest_version:
                continue
                
            # Extract current version for comparison
            current_version = current_runtime.split('/')[-1] if '/' in current_runtime else current_runtime
            
            # Compare versions
            if self.compare_versions(current_version, latest_version):
                logger.info(f"Runtime update available for {flatpak_id}: {current_version} -> {latest_version}")
                latest_runtime = current_runtime.replace(current_version, latest_version)
                
                # Add to outdated packages list
                outdated_package = {
                    "flatpak_id": flatpak_id,
                    "sources": flatpak_info.sources,
                    "current_runtime": current_runtime,
                    "latest_runtime": latest_runtime,
                    "current_version": current_version,
                    "latest_version": latest_version
                }
                outdated_packages.append(outdated_package)
            else:
                logger.info(f"{flatpak_id} runtime is up to date")
        
        logger.info(f"Runtime check complete. Found {len(outdated_packages)} outdated runtimes")
        
        # Save outdated packages to JSON file
        self.save_outdated_packages(outdated_packages, app_flatpaks)


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Check for flatpak runtime updates')
    parser.add_argument('--output', '-o', default='outdated_packages.json',
                       help='Output JSON file for outdated packages (default: outdated_packages.json)')
    args = parser.parse_args()
    
    checker = FlatpakRuntimeChecker(output_file=args.output)
    checker.check_runtime_updates()


if __name__ == '__main__':
    main()
