#!/usr/bin/env python3
"""
Interactive setup script for LLM JSON Processor
Helps users choose and install the right dependencies
"""

import subprocess
import sys
import os
from pathlib import Path


def run_command(cmd, description):
    """Run a shell command with error handling"""
    print(f"\n→ {description}")
    try:
        subprocess.check_call(cmd, shell=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed: {e}")
        return False


def check_ollama_installed():
    """Check if Ollama is installed"""
    try:
        result = subprocess.run(
            "ollama --version",
            shell=True,
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except:
        return False


def setup_ollama():
    """Setup for Ollama backend"""
    print("\n" + "="*60)
    print("OLLAMA SETUP")
    print("="*60)
    
    # Check if Ollama is installed
    if check_ollama_installed():
        print("✓ Ollama is already installed")
        installed = True
    else:
        print("Ollama not found. Instructions:")
        print("""
1. Download Ollama from https://ollama.ai
2. Install and run the application
3. Run 'ollama serve' in a terminal to start the server
        """)
        installed = input("Have you installed Ollama? (y/n): ").lower() == 'y'
    
    if installed:
        # Install Python dependencies
        print("\nInstalling Python dependencies...")
        if run_command(
            f"{sys.executable} -m pip install tqdm requests",
            "Installing tqdm and requests"
        ):
            print("✓ Dependencies installed")
            
            # Offer to pull a model
            pull_model = input("\nPull a model now? (y/n): ").lower() == 'y'
            if pull_model:
                print("\nAvailable models:")
                print("1. neural-chat:13b (Recommended - balanced)")
                print("2. mistral:13b (Fast inference)")
                print("3. llama2:13b (Best quality)")
                choice = input("\nChoose model (1-3) [default: 1]: ").strip() or "1"
                
                models = {
                    "1": "neural-chat:13b",
                    "2": "mistral:13b",
                    "3": "llama2:13b"
                }
                model = models.get(choice, "neural-chat:13b")
                
                print(f"\nPulling {model}...")
                print("(This may take a few minutes and ~8GB of disk space)")
                run_command(f"ollama pull {model}", f"Pulling {model}")


def setup_llama_cpp():
    """Setup for llama.cpp backend"""
    print("\n" + "="*60)
    print("LLAMA.CPP SETUP")
    print("="*60)
    
    print("""
llama.cpp is the fastest option for CPU inference.

Steps:
1. Install llama-cpp-python
2. Download a quantized model from Hugging Face
3. Run the processor with --backend llama-cpp
    """)
    
    install = input("Install llama-cpp-python? (y/n): ").lower() == 'y'
    if install:
        print("\nInstalling llama-cpp-python...")
        if run_command(
            f"{sys.executable} -m pip install llama-cpp-python",
            "Installing llama-cpp-python"
        ):
            print("✓ llama-cpp-python installed")
            
            download_model = input("\nDownload a model? (y/n): ").lower() == 'y'
            if download_model:
                print("\nRecommended models from Hugging Face TheBloke:")
                print("1. neural-chat-7b-v3-1 (8GB, fast)")
                print("2. mistral-7b-instruct (5GB, very fast)")
                print("3. llama2-13b-chat (8GB, powerful)")
                
                choice = input("\nChoose model (1-3) [default: 1]: ").strip() or "1"
                
                models = {
                    "1": {
                        "name": "neural-chat-7B-v3-1",
                        "url": "https://huggingface.co/TheBloke/neural-chat-7B-v3-1-GGUF/resolve/main/neural-chat-7b-v3-1.Q4_K_M.gguf"
                    },
                    "2": {
                        "name": "Mistral-7B-Instruct",
                        "url": "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.1-GGUF/resolve/main/mistral-7b-instruct-v0.1.Q4_K_M.gguf"
                    },
                    "3": {
                        "name": "Llama-2-13B-Chat",
                        "url": "https://huggingface.co/TheBloke/Llama-2-13B-chat-GGUF/resolve/main/llama-2-13b-chat.Q4_K_M.gguf"
                    }
                }
                
                model_info = models.get(choice, models["1"])
                print(f"\nDownloading {model_info['name']}...")
                print("(This may take a few minutes)")
                
                run_command(
                    f"wget -O models/{model_info['name']}.gguf \"{model_info['url']}\"",
                    f"Downloading {model_info['name']}"
                )


def test_setup():
    """Test the setup"""
    print("\n" + "="*60)
    print("TESTING SETUP")
    print("="*60)
    
    # Check for required packages
    required = ["tqdm"]
    optional = ["requests", "llama_cpp"]
    
    print("\nChecking required packages:")
    all_good = True
    for pkg in required:
        try:
            __import__(pkg)
            print(f"✓ {pkg}")
        except ImportError:
            print(f"✗ {pkg} not installed")
            all_good = False
    
    print("\nChecking optional packages:")
    for pkg in optional:
        try:
            __import__(pkg)
            print(f"✓ {pkg}")
        except ImportError:
            pkg_name = "llama-cpp-python" if pkg == "llama_cpp" else pkg
            print(f"✗ {pkg} not installed (optional)")
    
    if all_good:
        print("\n✓ Setup looks good!")
        return True
    else:
        print("\n✗ Some required packages are missing")
        return False


def main():
    print("="*60)
    print("LLM JSON Processor - Setup Assistant")
    print("="*60)
    
    print("""
This script will help you set up the LLM JSON Processor.

Choose your preferred backend:
1. Ollama (Recommended - easiest, supports GPU)
2. llama.cpp (Fastest on CPU, minimal dependencies)
3. Skip setup (already installed)
    """)
    
    choice = input("Choose option (1-3) [default: 1]: ").strip() or "1"
    
    if choice == "1":
        setup_ollama()
    elif choice == "2":
        setup_llama_cpp()
    elif choice == "3":
        print("Skipping setup")
    else:
        print("Invalid choice")
        return
    
    # Test setup
    if test_setup():
        print("\n" + "="*60)
        print("Ready to use! Run:")
        print("  python llm_json_processor.py <your-data.json>")
        print("="*60)
    else:
        print("\nPlease resolve the issues above and try again")


if __name__ == "__main__":
    main()