#
# Copyright 2015 Benjamin Kiessling
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""
kraken.rpred
~~~~~~~~~~~~

Generators for recognition on lines images.
"""
import logging
import numpy as np
import bidi.algorithm as bd

from abc import ABC, abstractmethod
from PIL import Image
from functools import partial
from collections import defaultdict
from typing import List, Tuple, Optional, Generator, Union, Dict, Sequence

from kraken.containers import BaselineOCRRecord, BBoxOCRRecord, ocr_record
from kraken.lib.util import get_im_str, is_bitonal
from kraken.lib.models import TorchSeqRecognizer
from kraken.lib.segmentation import extract_polygons, compute_polygon_section, Segmentation
from kraken.lib.exceptions import KrakenInputException
from kraken.lib.dataset import ImageInputTransforms

import copy

__all__ = ['mm_rpred', 'rpred']

logger = logging.getLogger(__name__)


class mm_rpred(object):
    """
    Multi-model version of kraken.rpred.rpred
    """
    def __init__(self,
                 nets: Dict[str, TorchSeqRecognizer],
                 im: Image.Image,
                 bounds: Segmentation,
                 pad: int = 16,
                 bidi_reordering: Union[bool, str] = True,
                 tags_ignore: Optional[List[str]] = None) -> Generator[ocr_record, None, None]:
        """
        Multi-model version of kraken.rpred.rpred.

        Takes a dictionary of ISO15924 script identifiers->models and an
        script-annotated segmentation to dynamically select appropriate models for
        these lines.

        Args:
            nets: A dict mapping tag values to TorchSegRecognizer objects.
                  Recommended to be an defaultdict.
            im: Image to extract text from
            bounds: A Segmentation data class containing either bounding box or
                    baseline type segmentation.
            pad: Extra blank padding to the left and right of text line
            bidi_reordering: Reorder classes in the ocr_record according to the
                             Unicode bidirectional algorithm for correct
                             display. Set to L|R to override default text
                             direction.
            tags_ignore: List of tag values to ignore during recognition

        Yields:
            An ocr_record containing the recognized text, absolute character
            positions, and confidence values for each character.

        Raises:
            KrakenInputException if the mapping between segmentation tags and
            networks is incomplete.
        """
        seg_types = set(recognizer.seg_type for recognizer in nets.values())
        if isinstance(nets, defaultdict):
            seg_types.add(nets.default_factory().seg_type)
            self._resolve_tags_to_model = partial(_resolve_tags_to_model, default=nets.default_factory())
        else:
            self._resolve_tags_to_model = _resolve_tags_to_model

        if not tags_ignore:
            tags_ignore = []

        if bounds.type not in seg_types or len(seg_types) > 1:
            logger.warning(f'Recognizers with segmentation types {seg_types} will be '
                           f'applied to segmentation of type {bounds.type}. '
                           f'This will likely result in severely degraded performace')
        one_channel_modes = set(recognizer.nn.one_channel_mode for recognizer in nets.values())
        if '1' in one_channel_modes and len(one_channel_modes) > 1:
            raise KrakenInputException('Mixing binary and non-binary recognition models is not supported.')
        elif '1' in one_channel_modes and not is_bitonal(im):
            logger.warning('Running binary models on non-binary input image '
                           f'(mode {im.mode}). This will result in severely degraded '
                           'performance')

        self.len = len(bounds.lines)
        self.line_iter = iter(bounds.lines)

        if bounds.type == 'baselines':
            valid_norm = False
            self.next_iter = self._recognize_baseline_line
        else:
            valid_norm = True
            self.next_iter = self._recognize_box_line

        tags = set()
        for x in bounds.lines:
            tags.update(x.tags.values())

        im_str = get_im_str(im)
        logger.info(f'Running {len(nets)} multi-script recognizers on {im_str} with {self.len} lines')

        filtered_tags = []
        miss = []
        for tag in tags:
            if not isinstance(nets, defaultdict) and (not nets.get(tag) and tag not in tags_ignore):
                miss.append(tag)
            elif tag not in tags_ignore:
                filtered_tags.append(tag)
        tags = filtered_tags

        if miss:
            raise KrakenInputException(f'Missing models for tags {set(miss)}')

        # build dictionary for line preprocessing
        self.ts = {}
        for tag in tags:
            logger.debug(f'Loading line transforms for {tag}')
            network = nets[tag]
            batch, channels, height, width = network.nn.input
            self.ts[tag] = ImageInputTransforms(batch, height, width, channels, (pad, 0), valid_norm)

        self.im = im
        self.nets = nets
        self.bidi_reordering = bidi_reordering
        self.pad = pad
        self.bounds = bounds
        self.tags_ignore = tags_ignore

    def _recognize_box_line(self, line):
        flat_box = [point for box in line.bbox for point in box]
        xmin, xmax = min(flat_box[::2]), max(flat_box[::2])
        ymin, ymax = min(flat_box[1::2]), max(flat_box[1::2])
        line_bbox = ((xmin, ymin), (xmin, ymax), (xmax, ymax), (xmax, ymin))
        prediction = ''
        cuts = []
        confidences = []
        line.text_direction = self.bounds.text_direction

        if self.tags_ignore is not None:
            for tag in line.tags.values():
                if tag in self.tags_ignore:
                    logger.info(f'Ignoring line segment with tags {line.tags} based on {tag}.')
                    return BaselineOCRRecord('', [], [], line)

        tag, net = self._resolve_tags_to_model(line.tags, self.nets)

        box, coords = next(extract_polygons(self.im, line))
        self.box = box

        # check if boxes are non-zero in any dimension
        if 0 in box.size:
            logger.warning(f'bbox {line} with zero dimension. Emitting empty record.')
            return BBoxOCRRecord('', (), (), line)
        # try conversion into tensor
        try:
            logger.debug('Preparing run.')
            ts_box = self.ts[tag](box)
        except Exception:
            logger.warning(f'Conversion of line {line} failed. Emitting empty record..')
            return BBoxOCRRecord('', (), (), line)

        # check if line is non-zero
        if ts_box.max() == ts_box.min():
            logger.warning('Empty run. Emitting empty record.')
            return BBoxOCRRecord('', (), (), line)

        _, net = self._resolve_tags_to_model({'type': tag}, self.nets)

        logger.debug(f'Forward pass with model {tag}.')
        preds = net.predict(ts_box.unsqueeze(0))[0]

        # calculate recognized LSTM locations of characters
        logger.debug('Convert to absolute coordinates')
        # calculate recognized LSTM locations of characters
        # scale between network output and network input
        self.net_scale = ts_box.shape[2]/net.outputs.shape[2]
        # scale between network input and original line
        self.in_scale = box.size[0]/(ts_box.shape[2]-2*self.pad)

        pred = ''.join(x[0] for x in preds)
        pos = []
        conf = []

        for _, start, end, c in preds:
            if self.bounds.text_direction.startswith('horizontal'):
                xmin = coords[0] + self._scale_val(start, 0, self.box.size[0])
                xmax = coords[0] + self._scale_val(end, 0, self.box.size[0])
                pos.append([[xmin, coords[1]], [xmin, coords[3]], [xmax, coords[3]], [xmax, coords[1]]])
            else:
                ymin = coords[1] + self._scale_val(start, 0, self.box.size[1])
                ymax = coords[1] + self._scale_val(end, 0, self.box.size[1])
                pos.append([[coords[0], ymin], [coords[2], ymin], [coords[2], ymax], [coords[0], ymax]])
            conf.append(c)
        prediction += pred
        cuts.extend(pos)
        confidences.extend(conf)

        rec = BBoxOCRRecord(prediction, cuts, confidences, line)
        if self.bidi_reordering:
            logger.debug('BiDi reordering record.')
            return rec.logical_order(base_dir=self.bidi_reordering if self.bidi_reordering in ('L', 'R') else None)
        else:
            logger.debug('Emitting raw record')
            return rec.display_order(None)

    def _recognize_baseline_line(self, line):
        if self.tags_ignore is not None:
            for tag in line.tags.values():
                if tag in self.tags_ignore:
                    logger.info(f'Ignoring line segment with tags {line.tags} based on {tag}.')
                    return BaselineOCRRecord('', [], [], line)

        try:
            box, coords = next(extract_polygons(self.im, line))
        except KrakenInputException as e:
            logger.warning(f'Extracting line failed: {e}')
            return BaselineOCRRecord('', [], [], line)

        self.box = box

        tag, net = self._resolve_tags_to_model(line.tags, self.nets)
        # check if boxes are non-zero in any dimension
        if 0 in box.size:
            logger.warning(f'{line} with zero dimension. Emitting empty record.')
            return BaselineOCRRecord('', [], [], line)
        # try conversion into tensor
        try:
            ts_box = self.ts[tag](box)
        except Exception as e:
            logger.warning(f'Tensor conversion failed with {e}. Emitting empty record.')
            return BaselineOCRRecord('', [], [], line)
        # check if line is non-zero
        if ts_box.max() == ts_box.min():
            logger.warning('Empty line after tensor conversion. Emitting empty record.')
            return BaselineOCRRecord('', [], [], line)

        preds = net.predict(ts_box.unsqueeze(0))[0]
        # calculate recognized LSTM locations of characters
        # scale between network output and network input
        self.net_scale = ts_box.shape[2]/net.outputs.shape[2]
        # scale between network input and original line
        self.in_scale = box.size[0]/(ts_box.shape[2]-2*self.pad)

        # XXX: fix bounding box calculation ocr_record for multi-codepoint labels.
        pred = ''.join(x[0] for x in preds)
        pos = []
        conf = []
        for _, start, end, c in preds:
            pos.append((self._scale_val(start, 0, self.box.size[0]),
                        self._scale_val(end, 0, self.box.size[0])))
            conf.append(c)
        rec = BaselineOCRRecord(pred, pos, conf, line)
        if self.bidi_reordering:
            logger.debug('BiDi reordering record.')
            return rec.logical_order(base_dir=self.bidi_reordering if self.bidi_reordering in ('L', 'R') else None)
        else:
            logger.debug('Emitting raw record')
            return rec.display_order(None)

    def __next__(self):
        bound = self.bounds
        return self.next_iter(next(self.line_iter))

    def __iter__(self):
        return self

    def __len__(self):
        return self.len

    def _scale_val(self, val, min_val, max_val):
        return int(round(min(max(((val*self.net_scale)-self.pad)*self.in_scale, min_val), max_val-1)))


def rpred(network: TorchSeqRecognizer,
          im: Image.Image,
          bounds: Segmentation,
          pad: int = 16,
          bidi_reordering: Union[bool, str] = True) -> Generator[ocr_record, None, None]:
    """
    Uses a TorchSeqRecognizer and a segmentation to recognize text

    Args:
        network: A TorchSegRecognizer object
        im: Image to extract text from
        bounds: A Segmentation class instance containing either a baseline or bbox segmentation.
        pad: Extra blank padding to the left and right of text line.
             Auto-disabled when expected network inputs are incompatible with
             padding.
        bidi_reordering: Reorder classes in the ocr_record according to the
                         Unicode bidirectional algorithm for correct display.
                         Set to L|R to change base text direction.

    Yields:
        An ocr_record containing the recognized text, absolute character
        positions, and confidence values for each character.
    """
    return mm_rpred(defaultdict(lambda: network), im, bounds, pad, bidi_reordering)


def _resolve_tags_to_model(tags: Sequence[Dict[str, str]],
                           model_map: Dict[str, TorchSeqRecognizer],
                           default: Optional[TorchSeqRecognizer] = None) -> TorchSeqRecognizer:
    """
    Resolves a sequence of tags
    """
    for tag in tags.values():
        if tag in model_map:
            return tag, model_map[tag]
    if default:
        return next(tags.values()), default
    raise KrakenInputException(f'No model for tags {tags}')
