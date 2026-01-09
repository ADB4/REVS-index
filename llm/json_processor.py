#!/usr/bin/env python3
"""
LLM JSON Processor - Infer missing values in JSON data using a local LLM
Supports both llama.cpp and Ollama backends

v2.0 - Smart field inference: only infers fields that are actually N/A
"""

import json
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass
from datetime import datetime

try:
    from tqdm import tqdm
except ImportError:
    print("tqdm not found. Install with: pip install tqdm --break-system-packages")
    # Fallback: simple progress indicator
    def tqdm(iterable, desc=""):
        total = len(iterable) if hasattr(iterable, '__len__') else None
        for i, item in enumerate(iterable):
            if total:
                print(f"\r{desc}: {i+1}/{total}", end="", flush=True)
            yield item
        print()


@dataclass
class Config:
    """Configuration for LLM processing"""
    input_file: str
    output_file: str
    model_type: str  # "ollama" or "llama-cpp"
    model_name: str  # e.g., "mistral:13b" or "neural-chat:13b"
    model_path: Optional[str] = None  # For llama-cpp, path to .gguf file
    batch_size: int = 1
    max_retries: int = 3
    temperature: float = 0.3
    top_p: float = 0.9
    verbose: bool = False
    fields_to_infer: Optional[List[str]] = None  # Which fields to infer
    interface: Optional[Dict[str, Any]] = None  # Field type/constraint schema
    dry_run: bool = False  # If True, only analyze data without LLM calls


class OllamaBackend:
    """Backend using Ollama API"""
    
    def __init__(self, model_name: str, temperature: float = 0.3, top_p: float = 0.9):
        try:
            import requests
        except ImportError:
            raise ImportError("requests required for Ollama backend. Install with: pip install requests")
        
        self.requests = requests
        self.model_name = model_name
        self.temperature = temperature
        self.top_p = top_p
        self.base_url = "http://localhost:11434"
        self._check_ollama()
    
    def _check_ollama(self):
        """Verify Ollama is running"""
        try:
            response = self.requests.get(f"{self.base_url}/api/tags", timeout=5)
            if response.status_code != 200:
                raise Exception("Ollama API returned non-200 status")
        except Exception as e:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self.base_url}\n"
                f"Make sure Ollama is running with: ollama serve\n"
                f"Error: {e}"
            )
    
    def generate(self, prompt: str) -> str:
        """Generate response from prompt"""
        try:
            response = self.requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                },
                timeout=300
            )
            response.raise_for_status()
            return response.json()["response"]
        except Exception as e:
            raise RuntimeError(f"Ollama generation failed: {e}")


class LlamaCppBackend:
    """Backend using llama-cpp-python"""
    
    def __init__(self, model_path: str, temperature: float = 0.3, top_p: float = 0.9):
        try:
            from llama_cpp import Llama
        except ImportError:
            raise ImportError(
                "llama-cpp-python required. Install with:\n"
                "pip install llama-cpp-python"
            )
        
        self.Llama = Llama
        self.model_path = model_path
        self.temperature = temperature
        self.top_p = top_p
        self.model = None
        self._load_model()
    
    def _load_model(self):
        """Load the model"""
        try:
            print(f"Loading model from {self.model_path}...")
            self.model = self.Llama(
                model_path=self.model_path,
                n_gpu_layers=-1,  # Use GPU if available
                n_ctx=2048,  # Context size
                verbose=False
            )
            print("Model loaded successfully")
        except Exception as e:
            raise RuntimeError(f"Failed to load model: {e}")
    
    def generate(self, prompt: str) -> str:
        """Generate response from prompt"""
        try:
            response = self.model(
                prompt,
                max_tokens=500,
                temperature=self.temperature,
                top_p=self.top_p,
            )
            return response["choices"][0]["text"]
        except Exception as e:
            raise RuntimeError(f"Model generation failed: {e}")


class JSONProcessor:
    """Process JSON entries with LLM"""
    
    # Fields that can be inferred
    INFERABLE_FIELDS = ["engine", "interior_color", "location", "seller_type"]
    
    def __init__(self, backend, config: Config):
        self.backend = backend
        self.config = config
        self.results = []
        self.errors = []
        self.original_entries = []
        
        # Stats tracking
        self.stats = {
            "total_entries": 0,
            "entries_with_na": 0,
            "entries_skipped": 0,
            "fields_inferred": {},  # field -> count
            "llm_calls": 0
        }
        
        # Determine which fields to infer
        if config.fields_to_infer:
            self.fields_to_infer = config.fields_to_infer
        else:
            self.fields_to_infer = self.INFERABLE_FIELDS
        
        # Initialize field stats
        for field in self.fields_to_infer:
            self.stats["fields_inferred"][field] = 0
        
        # Store interface for validation
        self.interface = config.interface or {}
    
    def _get_na_fields(self, entry: Dict[str, Any]) -> List[str]:
        """Get list of fields that are N/A in this entry (from fields_to_infer)"""
        na_fields = []
        for field in self.fields_to_infer:
            value = entry.get(field)
            # Check for N/A (string) or None or empty string
            if self._is_missing_value(value):
                na_fields.append(field)
        return na_fields
    
    def _is_missing_value(self, value: Any) -> bool:
        """Check if a value is considered 'missing' and needs inference"""
        if value is None:
            return True
        if isinstance(value, str):
            return value == "N/A" or value.strip() == ""
        return False
    
    def process_entry(self, entry: Dict[str, Any], index: int) -> Dict[str, Any]:
        """Process a single entry - only infer fields that are actually N/A"""
        result = {
            "index": index,
            "url": entry.get("url"),
            "title": entry.get("title"),
            "inferred_values": {},
            "fields_inferred": [],  # Track which fields were actually inferred
            "fields_skipped": [],   # Track which fields already had values
            "error": None
        }
        
        # Identify which fields actually need inference
        na_fields = self._get_na_fields(entry)
        non_na_fields = [f for f in self.fields_to_infer if f not in na_fields]
        
        result["fields_skipped"] = non_na_fields
        
        # Copy existing values for non-N/A fields
        for field in non_na_fields:
            result["inferred_values"][field] = entry.get(field)
        
        # If no fields need inference, skip LLM call entirely
        if not na_fields:
            self.stats["entries_skipped"] += 1
            if self.config.verbose:
                print(f"  Entry {index}: All fields populated, skipping LLM call")
            return result
        
        self.stats["entries_with_na"] += 1
        result["fields_inferred"] = na_fields
        
        if self.config.verbose:
            print(f"  Entry {index}: Inferring {len(na_fields)} field(s): {na_fields}")
        
        # Prepare prompt with only the N/A fields
        listing_details = "\n".join(entry.get("listing_details", []))
        excerpt = "\n".join(entry.get("excerpt", []))
        prompt = self._build_inference_prompt(listing_details, excerpt, na_fields)
        
        # Try to generate with retries
        for attempt in range(self.config.max_retries):
            try:
                self.stats["llm_calls"] += 1
                response = self.backend.generate(prompt)
                inferred = self._parse_response(response, na_fields)
                # Validate against constraints
                inferred = self._validate_inferred_values(inferred, na_fields)
                
                # Merge inferred values into result
                result["inferred_values"].update(inferred)
                
                # Update stats
                for field in na_fields:
                    if inferred.get(field) is not None:
                        self.stats["fields_inferred"][field] += 1
                
                return result
            except Exception as e:
                if attempt == self.config.max_retries - 1:
                    result["error"] = str(e)
                    self.errors.append((index, str(e)))
                    return result
        
        return result
    
    def _build_inference_prompt(self, listing_details: str, excerpt: str, na_fields: List[str]) -> str:
        """Build inference prompt for only the specified N/A fields"""
        # Build field descriptions from interface - only for N/A fields
        field_descriptions = []
        json_template_fields = []
        
        for field in na_fields:
            field_type = self.interface.get(field, "string")
            if isinstance(field_type, list):
                # Enum constraint
                options = ", ".join(f'"{opt}"' for opt in field_type)
                field_descriptions.append(f'    "{field}": one of [{options}]')
                json_template_fields.append(f'    "{field}": "..."')
            elif field_type == "integer":
                field_descriptions.append(f'    "{field}": integer')
                json_template_fields.append(f'    "{field}": 0')
            elif field_type == "boolean":
                field_descriptions.append(f'    "{field}": true or false')
                json_template_fields.append(f'    "{field}": true')
            elif field_type == "datetime":
                field_descriptions.append(f'    "{field}": datetime string (YYYY-MM-DD)')
                json_template_fields.append(f'    "{field}": "YYYY-MM-DD"')
            else:
                field_descriptions.append(f'    "{field}": string')
                json_template_fields.append(f'    "{field}": "..."')
        
        fields_spec = ",\n".join(field_descriptions)
        json_template = "{\n" + ",\n".join(json_template_fields) + "\n}"
        
        # Build a focused prompt
        if len(na_fields) == 1:
            field_word = "field"
            fields_list = f'"{na_fields[0]}"'
        else:
            field_word = "fields"
            fields_list = ", ".join(f'"{f}"' for f in na_fields)
        
        prompt = f"""Given this car listing data, infer the missing {field_word} ({fields_list}) based on the listing_details and excerpt.

Listing Details:
{listing_details}

Excerpt:
{excerpt}

Extract and return ONLY a valid JSON object with these {len(na_fields)} {field_word}:
{fields_spec}

For enum fields, use ONLY the specified values.
For other fields, use null if you cannot infer the value.

Return ONLY the JSON object in this format, no other text:
{json_template}"""
        
        return prompt
    
    def _validate_inferred_values(self, inferred: Dict[str, Any], na_fields: List[str]) -> Dict[str, Any]:
        """Validate inferred values against interface constraints"""
        validated = {}
        
        for field in na_fields:
            value = inferred.get(field)
            field_type = self.interface.get(field, "string")
            
            # Check enum constraints
            if isinstance(field_type, list):
                # This is an enum constraint
                if value not in field_type and value is not None:
                    # Value doesn't match enum, set to null
                    validated[field] = None
                else:
                    validated[field] = value
            elif field_type == "integer":
                # Try to convert to int
                if value is not None:
                    try:
                        validated[field] = int(value)
                    except (ValueError, TypeError):
                        validated[field] = None
                else:
                    validated[field] = None
            elif field_type == "boolean":
                # Handle boolean
                if isinstance(value, bool):
                    validated[field] = value
                elif isinstance(value, str):
                    validated[field] = value.lower() in ("true", "yes", "1")
                else:
                    validated[field] = None
            else:
                validated[field] = value
        
        return validated
    
    def _parse_response(self, response: str, expected_fields: List[str]) -> Dict[str, Any]:
        """Parse JSON response from LLM"""
        import re
        
        # Try to extract JSON from response
        # Look for JSON object pattern
        json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                # Ensure all expected fields are present
                for field in expected_fields:
                    if field not in parsed:
                        parsed[field] = None
                return parsed
            except json.JSONDecodeError:
                pass
        
        # Try parsing entire response
        try:
            parsed = json.loads(response)
            for field in expected_fields:
                if field not in parsed:
                    parsed[field] = None
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse response as JSON: {response[:200]}...")
    
    def process_file(self) -> None:
        """Process entire JSON file"""
        # Validate and create output directory
        output_path = Path(self.config.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Load input
        print(f"Loading {self.config.input_file}...")
        with open(self.config.input_file, 'r') as f:
            entries = json.load(f)
        
        # Store original entries
        self.original_entries = entries
        self.stats["total_entries"] = len(entries)
        
        # Pre-analyze the data
        self._analyze_data(entries)
        
        print(f"Processing {len(entries)} entries...")
        print(f"Fields to infer: {self.fields_to_infer}")
        
        # Process with progress bar
        for idx, entry in enumerate(tqdm(entries, desc="Processing")):
            result = self.process_entry(entry, idx)
            self.results.append(result)
        
        # Save results
        self._save_results()
        
        # Print summary
        self._print_summary()
    
    def _analyze_data(self, entries: List[Dict[str, Any]]) -> None:
        """Pre-analyze data to show N/A distribution"""
        print("\n" + "="*60)
        print("Data Analysis")
        print("="*60)
        
        na_counts = {field: 0 for field in self.fields_to_infer}
        entries_needing_inference = 0
        
        for entry in entries:
            na_fields = self._get_na_fields(entry)
            if na_fields:
                entries_needing_inference += 1
            for field in na_fields:
                na_counts[field] += 1
        
        print(f"Total entries: {len(entries)}")
        print(f"Entries with at least one N/A field: {entries_needing_inference}")
        print(f"Entries with all fields populated: {len(entries) - entries_needing_inference}")
        print("\nN/A count per field:")
        for field, count in na_counts.items():
            pct = (count / len(entries)) * 100 if entries else 0
            print(f"  {field}: {count} ({pct:.1f}%)")
        print("="*60 + "\n")
        
        return na_counts, entries_needing_inference
    
    def dry_run(self) -> None:
        """Analyze data without making LLM calls - show what would be inferred"""
        # Load input
        print(f"Loading {self.config.input_file}...")
        with open(self.config.input_file, 'r') as f:
            entries = json.load(f)
        
        self.stats["total_entries"] = len(entries)
        
        # Analyze the data
        na_counts, entries_needing_inference = self._analyze_data(entries)
        
        print("\n" + "="*60)
        print("DRY RUN - Inference Plan")
        print("="*60)
        
        total_field_inferences = sum(na_counts.values())
        old_approach_inferences = entries_needing_inference * len(self.fields_to_infer)
        
        print(f"\nWith smart field inference:")
        print(f"  LLM calls needed: {entries_needing_inference}")
        print(f"  Total fields to infer: {total_field_inferences}")
        
        if old_approach_inferences > 0:
            print(f"\nWithout smart field inference (old approach):")
            print(f"  LLM calls needed: {entries_needing_inference}")
            print(f"  Total fields to infer: {old_approach_inferences}")
            savings = ((old_approach_inferences - total_field_inferences) / old_approach_inferences) * 100
            print(f"\n  Efficiency gain: {savings:.1f}% fewer field inferences")
        
        # Show first few entries that need inference
        print(f"\n{'='*60}")
        print(f"Sample entries needing inference:")
        print(f"{'='*60}")
        
        shown = 0
        for idx, entry in enumerate(entries):
            na_fields = self._get_na_fields(entry)
            if na_fields and shown < 5:
                print(f"\nEntry {idx}: {entry.get('title', 'N/A')[:50]}")
                print(f"  Fields to infer: {na_fields}")
                shown += 1
        
        if entries_needing_inference > 5:
            print(f"\n  ... and {entries_needing_inference - 5} more entries")
        
        print(f"\n{'='*60}")
        print("To run actual inference, remove --dry-run flag")
        print("="*60)
    
    def _get_updated_entries(self) -> List[Dict[str, Any]]:
        """Merge inferred values back into original entries"""
        updated_entries = []
        
        for result in self.results:
            idx = result["index"]
            original_entry = self.original_entries[idx]
            
            # Create a copy of the original entry
            updated_entry = original_entry.copy()
            
            # Merge inferred values if successful
            if result["error"] is None and result["inferred_values"]:
                for field in result.get("fields_inferred", []):
                    inferred_value = result["inferred_values"].get(field)
                    # Only replace if original was N/A and we have an inferred value
                    if original_entry.get(field) == "N/A" and inferred_value is not None:
                        updated_entry[field] = inferred_value
            
            updated_entries.append(updated_entry)
        
        return updated_entries
    
    def _save_results(self) -> None:
        """Save results to JSON file"""
        # Create detailed results structure
        output = {
            "timestamp": datetime.now().isoformat(),
            "config": {
                "input_file": self.config.input_file,
                "model_type": self.config.model_type,
                "model_name": self.config.model_name,
                "fields_to_infer": self.fields_to_infer,
            },
            "stats": self.stats,
            "results": self.results,
            "errors": [{"index": idx, "message": msg} for idx, msg in self.errors]
        }
        
        with open(self.config.output_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        print(f"\nDetailed results saved to {self.config.output_file}")
        
        # Also save updated entries with inferred values merged in
        updated_entries = self._get_updated_entries()
        updated_output_file = self.config.output_file.replace('.json', '_updated.json')
        
        with open(updated_output_file, 'w') as f:
            json.dump(updated_entries, f, indent=2)
        
        print(f"Updated entries saved to {updated_output_file}")
    
    def _print_summary(self) -> None:
        """Print processing summary"""
        successful = len([r for r in self.results if r["error"] is None])
        failed = len([r for r in self.results if r["error"] is not None])
        
        print(f"\n{'='*60}")
        print(f"Processing Summary")
        print(f"{'='*60}")
        print(f"Total entries: {self.stats['total_entries']}")
        print(f"Entries needing inference: {self.stats['entries_with_na']}")
        print(f"Entries skipped (no N/A): {self.stats['entries_skipped']}")
        print(f"LLM calls made: {self.stats['llm_calls']}")
        print(f"Successful: {successful}")
        print(f"Failed: {failed}")
        
        print(f"\nFields inferred (non-null values):")
        for field, count in self.stats["fields_inferred"].items():
            print(f"  {field}: {count}")
        
        if failed > 0:
            print(f"\nFirst few errors:")
            for idx, msg in self.errors[:3]:
                print(f"  Entry {idx}: {msg[:80]}")


def main():
    parser = argparse.ArgumentParser(
        description="Process JSON file with local LLM to infer missing values"
    )
    parser.add_argument(
        "input_file",
        help="Input JSON file to process"
    )
    parser.add_argument(
        "-o", "--output",
        default="llm_output.json",
        help="Output JSON file (default: llm_output.json)"
    )
    parser.add_argument(
        "--backend",
        choices=["ollama", "llama-cpp"],
        default="ollama",
        help="LLM backend to use (default: ollama)"
    )
    parser.add_argument(
        "--model",
        default="neural-chat:13b",
        help="Model name for Ollama or path to .gguf file for llama-cpp"
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="LLM temperature (0-1, default: 0.3)"
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.9,
        help="LLM top_p (0-1, default: 0.9)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "--fields",
        default=None,
        help="Comma-separated list of fields to infer (default: all inferable fields)"
    )
    parser.add_argument(
        "--interface",
        default=None,
        help="Path to JSON file specifying field types and constraints (TypeScript-like interface)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze data and show what would be inferred without making LLM calls"
    )
    
    args = parser.parse_args()
    
    # Validate input file
    if not Path(args.input_file).exists():
        print(f"Error: Input file '{args.input_file}' not found")
        sys.exit(1)
    
    # Validate and create output directory
    output_path = Path(args.output)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Test write permissions
        test_file = output_path.parent / ".test_write"
        test_file.touch()
        test_file.unlink()
        print(f"Output directory: {output_path.parent}")
    except Exception as e:
        print(f"Error: Cannot write to output directory '{output_path.parent}'")
        print(f"Details: {e}")
        sys.exit(1)
    
    # Parse fields to infer
    fields_to_infer = None
    if args.fields:
        fields_to_infer = [f.strip() for f in args.fields.split(",")]
        print(f"Fields to infer: {fields_to_infer}")
    
    # Load interface schema if provided
    interface = None
    if args.interface:
        if not Path(args.interface).exists():
            print(f"Error: Interface file '{args.interface}' not found")
            sys.exit(1)
        try:
            with open(args.interface, 'r') as f:
                interface = json.load(f)
            print(f"Loaded interface schema from {args.interface}")
        except json.JSONDecodeError as e:
            print(f"Error: Failed to parse interface file as JSON: {e}")
            sys.exit(1)
    
    # Create config
    config = Config(
        input_file=args.input_file,
        output_file=args.output,
        model_type=args.backend,
        model_name=args.model,
        model_path=args.model if args.backend == "llama-cpp" else None,
        temperature=args.temperature,
        top_p=args.top_p,
        verbose=args.verbose,
        fields_to_infer=fields_to_infer,
        interface=interface,
        dry_run=args.dry_run
    )
    
    # Handle dry run mode
    if args.dry_run:
        print("\n*** DRY RUN MODE - No LLM calls will be made ***\n")
        processor = JSONProcessor(None, config)  # No backend needed
        processor.dry_run()
        sys.exit(0)
    
    # Initialize backend
    try:
        if config.model_type == "ollama":
            print(f"Using Ollama backend with model: {config.model_name}")
            backend = OllamaBackend(config.model_name, config.temperature, config.top_p)
        else:
            print(f"Using llama.cpp backend with model: {config.model_path}")
            backend = LlamaCppBackend(config.model_path, config.temperature, config.top_p)
    except Exception as e:
        print(f"Error initializing backend: {e}")
        sys.exit(1)
    
    # Process
    processor = JSONProcessor(backend, config)
    try:
        processor.process_file()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        processor._save_results()
        sys.exit(0)
    except Exception as e:
        print(f"Error during processing: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()