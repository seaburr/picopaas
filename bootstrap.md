# PaaS Project "PicoPaaS"

Every prompt in this pack starts by pasting this conteact. It is no-negotiable. A run that breaks it gets thrown away.

## Mission

You are an engineering team tasked with researching, designing, documenting, building, testing, running, and securing a PaaS which mirrors what a platform like Heroku offers though meant for very quick basic throwaway projects. When finally need access to DigitalOcean, you will work through Chas, the leader of this project. Any infra that you need to deploy will require a simple:

1 - What is being deployed.
2 - Why in a sentence is this needed.
3 - Cost: What will this cost monthly (static cost + cost per platform deployed)

## PaaS Platform Offerings

A basic platform that let's an engineer deploy a single Docker image, wire up environment variables/secrets, and request a SQL database and S3-compatible blob storage. 

DB size is capped at 50MB.
S3-compatible storage capped at 100MB
Compute is capped at 100m (1/10th core)
RAM is capped at 384MB
Image size is capped at 512MB
Scatch Disk: 250MB

## Hosting Underpinning

While this will require some research to validate, the goal is to host this on DigitalOcean using the following technologies:

- MySQL
- Spaces (S3-compatible storage)
- Kubernetes

The hope is we can reuse a single MySQL server to host several databases but keep them isolated from each other. This goes for k8s clusters. Divide deployments by namespaces.

### Platform Tech

Platform should, ideally, be written in Python (backend) and Vue (frontend). As a model of another project written that could be useful for shaping UI design and standards around testing, please look at: https://github.com/seaburr/tribal

## Using the Platform

A user must create an account on the platform and then they can proceed to deploy a single app (optionally with storage and DB if wanted). This can be deployed via a simple UI or via a basic YAML spec we provide. If they request storage and/or DB we will expose those as reference variables they can map to the environment variables they need for their application to run. With the exception of the uberadmin role (which the first user of the platform gets), they can only deploy a single app. This should be configurable in the future by the uberadmin later (allow specific users to deploy mutltiple). 

## DNS

Use seaburr.io as the domain which already has it's nameservers configured in DigitalOcean. A deployed app should be accessible at: https://random-pet-12345.seaburr.io where the random pet is something like "clever-robin" and the 12345 is some sort of 5 characters of random letters and numbers safe for a URL.

## Budget

This project is on an extremely tight budget both from a build budget but also a hosting budget. The goal is to have a usable hosting platform up and running with all the trimming needed to host storage and DBs for under $40/mo including the first few users of this service. This is being built on a Claude Pro account so expect to run out of tokens and need to stop often. You should keep a ledger of work being done and spawn small agents to do reasonable chunks when possible so work is not lost when tokens are gone.

## Tracking Notes

However you choose to track this, all work/notes/code needs to go into /Users/cberndt/source/picopaas which is Git repo hosted in GitHub at https://github.com/seaburr/picopaas.git. I want one agent managing pushes to the repo and watching GitHub test pipelines as the project gets written. This same agent should also be reponsible for ensuring environment variables are not checked into SCM. Anything we need, you can work with Chas on tight requests for ("Add DigitalOcean API key to repo secrets") and he will handle when he can.

## Agents

To keep token burn to a minimum, I propose the following agent structure though I leave it to you to design the agents:

1. Manager - Track progress and handle comms with Chas.
2. SCM / Security - Watches all things security related including proposing issues for the development agents to test. This agent also handles SCM pushes to keep agents from trying to push code and stepping on each others feet.
3. Backend Dev
4. Frontend Dev
5. Infra/Ops engineer
