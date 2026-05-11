"""
Zone utilities for domain apex detection using manual parsing.
"""
import logging

logger = logging.getLogger(__name__)

def to_apex(domain: str) -> str:
    """
    Convert any domain (including subdomains) to its registrable apex domain.
    Uses a manual whitelist of multi-part TLDs to avoid dependency issues.
    
    Examples:
        mail.team.example.co.uk -> example.co.uk
        sub.example.com -> example.com
        example.com -> example.com
        jemmie.amasahistoricalsociety.space -> amasahistoricalsociety.space
    
    Args:
        domain: Input domain (can be subdomain or apex)
    
    Returns:
        Apex domain string
    """
    try:
        domain = domain.strip().lower()
        if not domain:
            raise ValueError("Empty domain string")
        
        # Manual parsing strategy to avoid publicsuffix2 issues
        # 1. Define known multi-part TLDs
        known_multipart_tlds = {
            # UK TLDs
            'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'me.uk', 'ltd.uk', 'plc.uk', 'net.uk', 'sch.uk',
            # Australia TLDs
            'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au',
            # New Zealand TLDs
            'co.nz', 'net.nz', 'org.nz',
            # Japan TLDs
            'co.jp', 'ne.jp', 'or.jp', 'go.jp', 'ac.jp',
            # Brazil TLDs
            'com.br', 'net.br', 'org.br', 'gov.br',
            # Singapore TLDs
            'com.sg', 'edu.sg', 'gov.sg', 'net.sg', 'org.sg',
            # South Africa TLDs
            'co.za', 'org.za', 'gov.za',
            # India TLDs
            'co.in', 'net.in', 'org.in', 'gen.in', 'ind.in',
            # China TLDs
            'com.cn', 'net.cn', 'org.cn', 'gov.cn',
            # Country + Generic Combo TLDs (important for verification!)
            'it.com', 'eu.com', 'us.com', 'uk.com', 'de.com', 'fr.com', 'es.com', 'ru.com',
            'br.com', 'cn.com', 'ar.com', 'kr.com', 'hu.com', 'no.com', 'se.com', 'za.com',
            'uk.net', 'gb.net', 'jp.net', 'eu.net',
            # Other multi-part TLDs
            'com.mx', 'org.mx', 'edu.mx', 'gob.mx',
            'co.il', 'org.il', 'net.il', 'ac.il', 'gov.il',
            'com.tr', 'net.tr', 'org.tr', 'edu.tr', 'gov.tr',
            'com.ar', 'net.ar', 'org.ar', 'edu.ar', 'gob.ar',
            'co.kr', 'or.kr', 'go.kr', 'ac.kr', 're.kr',
            'com.hk', 'org.hk', 'net.hk', 'edu.hk', 'gov.hk',
            'com.my', 'net.my', 'org.my', 'edu.my', 'gov.my',
            'com.ph', 'net.ph', 'org.ph', 'edu.ph', 'gov.ph',
            'com.tw', 'net.tw', 'org.tw', 'edu.tw', 'gov.tw',
            'co.th', 'or.th', 'in.th', 'ac.th', 'go.th',
            'com.ua', 'net.ua', 'org.ua', 'edu.ua', 'gov.ua',
            'com.eg', 'org.eg', 'net.eg', 'edu.eg', 'gov.eg',
            'co.ke', 'or.ke', 'ne.ke', 'ac.ke', 'go.ke',
            'com.ng', 'org.ng', 'net.ng', 'edu.ng', 'gov.ng'
        }
        
        parts = domain.split('.')
        if len(parts) < 2:
            return domain
            
        # Check if the last two parts form a known multi-part TLD
        last_two = '.'.join(parts[-2:])
        
        if last_two in known_multipart_tlds:
            # It's a multi-part TLD (e.g. example.co.uk)
            # We need at least 3 parts (SLD + TLD part 1 + TLD part 2)
            if len(parts) >= 3:
                apex = '.'.join(parts[-3:])
                logger.info(f"to_apex (manual): {domain} -> {apex} (Multi-part TLD)")
                return apex
            return domain # Return as is if not enough parts
            
        # Standard TLD (e.g. example.com, example.space)
        # Apex is last 2 parts
        apex = '.'.join(parts[-2:])
        logger.info(f"to_apex (manual): {domain} -> {apex} (Standard TLD)")
        return apex
        
    except Exception as e:
        logger.error(f"Error converting {domain} to apex: {e}")
        # Fallback: basic domain extraction
        parts = domain.strip().lower().split('.')
        if len(parts) >= 2:
            return '.'.join(parts[-2:])
        return domain

def matching_zone_in_namecheap(apex: str) -> str:
    """
    For Namecheap, the apex domain is the zone.
    This function validates and returns the zone name.
    
    Args:
        apex: Apex domain name
    
    Returns:
        Zone name (same as apex for Namecheap)
    """
    apex = apex.strip().lower()
    if not apex:
        raise ValueError("Empty apex domain")
    
    # Namecheap uses the apex as the zone
    return apex
